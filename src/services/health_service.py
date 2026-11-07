"""Operational health details exposed to administrators."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.config import Settings
from src.database.connection import Database
from src.errors import forbidden
from src.security.tokens import Principal


class HealthService:
    async def details(
        self, principal: Principal, database: Database, settings: Settings
    ) -> dict[str, Any]:
        if not principal.is_admin:
            raise forbidden("insufficient_role")
        async with database.engine.connect() as conn:
            task_rows = (
                (
                    await conn.execute(
                        text("SELECT status,count(*) AS count FROM tasks GROUP BY status")
                    )
                )
                .mappings()
                .all()
            )
            event_rows = (
                (
                    await conn.execute(
                        text("SELECT status,count(*) AS count FROM events_outbox GROUP BY status")
                    )
                )
                .mappings()
                .all()
            )
            journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one()

        heartbeat = Path("/run/agenthub/worker_heartbeat")
        try:
            age = max(0.0, time.time() - heartbeat.stat().st_mtime)
        except FileNotFoundError:
            age = None
        wal_path = Path(str(settings.database_path) + "-wal")
        try:
            wal_size = wal_path.stat().st_size
        except FileNotFoundError:
            wal_size = 0
        return {
            "database": {
                "status": "ok",
                "journal_mode": journal,
                "wal_size_bytes": wal_size,
            },
            "worker": {"heartbeat_age_seconds": round(age, 1) if age is not None else None},
            "task_queue": {row["status"]: row["count"] for row in task_rows},
            "outbox": {row["status"]: row["count"] for row in event_rows},
        }


health_service = HealthService()
