"""Durable event polling and one shared SSE outbox tailer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from src.database.connection import Database
from src.database.connection import db as default_db
from src.security.tokens import Principal
from src.services.common import all_rows, require_admin, require_project_access, resolve_project


@dataclass(eq=False)
class Subscriber:
    project_ids: frozenset[str]
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=256))


class EventService:
    def __init__(self, database: Database | None = None, max_connections: int = 20):
        self.db = database or default_db
        self.max_connections = max_connections
        self.subscribers: set[Subscriber] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_seq = 0

    async def start(self) -> None:
        async with self.db.engine.connect() as conn:
            self._last_seq = int(
                (
                    await conn.execute(text("SELECT COALESCE(max(seq),0) FROM events_outbox"))
                ).scalar_one()
            )
        self._stop.clear()
        self._task = asyncio.create_task(self._tail(), name="events-outbox-tailer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.subscribers.clear()

    async def subscribe(self, principal: Principal, project_ref: str | None = None) -> Subscriber:
        if len(self.subscribers) >= self.max_connections:
            raise RuntimeError("too many event streams")
        async with self.db.engine.connect() as conn:
            if principal.is_agent:
                projects = await all_rows(
                    conn,
                    """SELECT pa.project_id FROM project_agents pa JOIN projects p ON p.id=pa.project_id
                       WHERE pa.agent_id=:agent AND p.is_active=1""",
                    {"agent": principal.id},
                )
                project_ids = {r["project_id"] for r in projects}
            elif principal.is_admin:
                projects = await all_rows(
                    conn, "SELECT id AS project_id FROM projects WHERE is_active=1"
                )
                project_ids = {r["project_id"] for r in projects}
            else:
                projects = await all_rows(
                    conn,
                    """SELECT upa.project_id FROM user_project_access upa JOIN projects p ON p.id=upa.project_id
                       WHERE upa.user_id=:user AND p.is_active=1""",
                    {"user": principal.id},
                )
                project_ids = {r["project_id"] for r in projects}
            if project_ref:
                project = await resolve_project(conn, project_ref)
                await require_project_access(conn, project["id"], principal)
                project_ids &= {project["id"]}
        subscriber = Subscriber(frozenset(project_ids))
        self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    async def _tail(self) -> None:
        while not self._stop.is_set():
            try:
                rows = await self.db.fetch_all(
                    "SELECT seq,id,project_id,event_type,aggregate_type,aggregate_id,payload,created_at "
                    "FROM events_outbox WHERE seq>:seq ORDER BY seq LIMIT 500",
                    {"seq": self._last_seq},
                )
                for row in rows:
                    self._last_seq = int(row["seq"])
                    if row["payload"]:
                        row["payload"] = json.loads(row["payload"])
                    for subscriber in tuple(self.subscribers):
                        if row["project_id"] is None or row["project_id"] in subscriber.project_ids:
                            self._offer(subscriber, row)
            except asyncio.CancelledError:
                raise
            except Exception:
                # SSE is a best-effort view over the durable log; a later poll resumes.
                await asyncio.sleep(1)
            await asyncio.sleep(1)

    @staticmethod
    def _offer(subscriber: Subscriber, event: dict[str, Any]) -> None:
        if subscriber.queue.full():
            while not subscriber.queue.empty():
                try:
                    subscriber.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            subscriber.queue.put_nowait({"event": "resync", "seq": event["seq"]})
            return
        subscriber.queue.put_nowait(event)

    async def events_after(
        self, principal: Principal, after_seq: int, limit: int = 100, project_ref: str | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        async with self.db.engine.connect() as conn:
            if principal.is_agent:
                access = await all_rows(
                    conn,
                    """SELECT pa.project_id FROM project_agents pa JOIN projects p ON p.id=pa.project_id
                       WHERE pa.agent_id=:agent AND p.is_active=1""",
                    {"agent": principal.id},
                )
            elif principal.is_admin:
                access = await all_rows(
                    conn, "SELECT id AS project_id FROM projects WHERE is_active=1"
                )
            else:
                access = await all_rows(
                    conn,
                    """SELECT upa.project_id FROM user_project_access upa JOIN projects p ON p.id=upa.project_id
                       WHERE upa.user_id=:user AND p.is_active=1""",
                    {"user": principal.id},
                )
            ids = {r["project_id"] for r in access}
            if project_ref:
                project = await resolve_project(conn, project_ref)
                await require_project_access(conn, project["id"], principal)
                ids &= {project["id"]}
            minimum = int(
                (
                    await conn.execute(text("SELECT COALESCE(min(seq),0) FROM events_outbox"))
                ).scalar_one()
            )
            rows = await all_rows(
                conn,
                """SELECT seq,id,project_id,event_type,aggregate_type,aggregate_id,payload,created_at
                                     FROM events_outbox WHERE seq>:seq ORDER BY seq LIMIT :limit""",
                {"seq": after_seq, "limit": limit * 3},
            )
        visible = []
        scanned_seq = after_seq
        for row in rows:
            scanned_seq = int(row["seq"])
            if row["project_id"] is not None and row["project_id"] not in ids:
                continue
            row["payload"] = json.loads(row["payload"]) if row["payload"] else {}
            visible.append(row)
            if len(visible) >= limit:
                break
        return {
            "events": visible,
            "next_seq": scanned_seq,
            "resync_required": bool(minimum and after_seq < minimum - 1),
        }

    async def failed_outbox_events(
        self, principal: Principal, limit: int = 25
    ) -> dict[str, list[dict[str, Any]]]:
        require_admin(principal)
        limit = max(1, min(limit, 100))
        async with self.db.engine.connect() as conn:
            events = await all_rows(
                conn,
                """SELECT e.seq,e.id,e.project_id,p.slug AS project,e.event_type,e.aggregate_type,
                          e.aggregate_id,e.status,e.attempt_count,e.last_error,e.created_at
                   FROM events_outbox e LEFT JOIN projects p ON p.id=e.project_id
                   WHERE e.status='failed' ORDER BY e.created_at DESC,e.seq DESC LIMIT :limit""",
                {"limit": limit},
            )
        return {"events": events}


event_service = EventService(max_connections=20)
