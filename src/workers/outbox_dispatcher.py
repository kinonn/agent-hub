from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from sqlalchemy import text

from src.adapters.base import AgentRecord, TaskSummary
from src.adapters.http import HTTPWakeupAdapter
from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_after, utc_now
from src.services.common import all_rows, one


class OutboxDispatcher:
    def __init__(self, database: Database | None = None, settings: Settings | None = None):
        self.db = database or default_db
        self.settings = settings or get_settings()
        self.adapter = HTTPWakeupAdapter(timeout_seconds=30)

    async def dispatch_batch(self, batch_size: int = 20) -> int:
        now = utc_now()
        async with self.db.write_tx() as tx:
            # A crashed worker must not strand processing events permanently.
            await tx.execute(
                text("""UPDATE events_outbox SET status='pending',processing_started_at=NULL
                       WHERE status='processing' AND processing_started_at<:cutoff"""),
                {"cutoff": utc_after(-self.settings.outbox_processing_timeout_seconds, now)},
            )
            events = await all_rows(
                tx,
                """SELECT * FROM events_outbox WHERE status='pending' AND next_attempt_at<=:now
                   ORDER BY seq LIMIT :limit""",
                {"now": now, "limit": batch_size},
            )
            for event in events:
                await tx.execute(
                    text("""UPDATE events_outbox SET status='processing',processing_started_at=:now,
                           last_attempt_at=:now,attempt_count=attempt_count+1 WHERE seq=:seq AND status='pending'"""),
                    {"now": now, "seq": event["seq"]},
                )
        for event in events:
            try:
                await self._deliver(event)
            except Exception as exc:
                await self._failed(event, type(exc).__name__)
            else:
                async with self.db.write_tx() as tx:
                    await tx.execute(
                        text("""UPDATE events_outbox SET status='delivered',delivered_at=:now,
                               processing_started_at=NULL,last_error=NULL WHERE seq=:seq AND status='processing'"""),
                        {"now": utc_now(), "seq": event["seq"]},
                    )
        return len(events)

    async def _deliver(self, event: dict[str, Any]) -> None:
        if (
            not event["project_id"]
            or event["aggregate_type"] != "task"
            or event["event_type"] != "task.available"
        ):
            return
        async with self.db.engine.connect() as conn:
            targets = await all_rows(
                conn,
                """SELECT a.id,a.name,a.adapter_config FROM project_agents pa JOIN agents a ON a.id=pa.agent_id
                   JOIN projects p ON p.id=pa.project_id
                   WHERE pa.project_id=:project AND p.is_active=1 AND pa.role IN ('contributor','maintainer')
                     AND a.status='active' AND a.execution_mode='adapter'""",
                {"project": event["project_id"]},
            )
            if event["aggregate_type"] == "task":
                task = await one(
                    conn,
                    "SELECT id,title,status,assigned_agent_id FROM tasks WHERE id=:id",
                    {"id": event["aggregate_id"]},
                )
            else:
                task = None
        if task is None:
            return
        if task and task.get("assigned_agent_id"):
            targets = [agent for agent in targets if agent["id"] == task["assigned_agent_id"]]
        if not targets:
            return
        summary = TaskSummary(
            id=event["aggregate_id"],
            title=(task or {}).get("title", event["event_type"]),
            status=(task or {}).get("status", event["event_type"]),
        )
        failures: list[str] = []
        for target in targets:
            config = json.loads(target["adapter_config"] or "{}")
            config["event_id"] = event["id"]
            agent = AgentRecord(id=target["id"], name=target["name"], adapter_config=config)
            try:
                result = await asyncio.wait_for(self.adapter.dispatch(agent, summary), timeout=30)
            except Exception as exc:
                failures.append(type(exc).__name__)
            else:
                if not result.delivered:
                    failures.append(result.detail or "adapter delivery failed")
        if failures:
            raise RuntimeError("; ".join(failures)[:300])

    async def _failed(self, event: dict[str, Any], detail: str) -> None:
        now = utc_now()
        attempts = int(event["attempt_count"]) + 1
        terminal = attempts >= self.settings.outbox_max_attempts
        delay = min(300, 5 * (2 ** min(attempts, 6))) + random.randint(0, 5)
        async with self.db.write_tx() as tx:
            await tx.execute(
                text("""UPDATE events_outbox SET status=:status,last_error=:error,processing_started_at=NULL,
                       next_attempt_at=:next WHERE seq=:seq AND status='processing'"""),
                {
                    "status": "failed" if terminal else "pending",
                    "error": detail[:300],
                    "next": None if terminal else utc_after(delay, now),
                    "seq": event["seq"],
                },
            )


outbox_dispatcher = OutboxDispatcher()


async def dispatch_outbox() -> int:
    return await outbox_dispatcher.dispatch_batch()
