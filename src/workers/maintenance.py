from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_after, utc_now


class Maintenance:
    def __init__(self, database: Database | None = None, settings: Settings | None = None):
        self.db = database or default_db
        self.settings = settings or get_settings()
        self.last_maintenance_day: str | None = None
        self.last_optimize_day: str | None = None

    async def run_once(self) -> None:
        await self.prune()
        today = datetime.now(UTC).date().isoformat()
        async with self.db.engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
            if self.last_optimize_day != today:
                await conn.exec_driver_sql("PRAGMA optimize")
                self.last_optimize_day = today
        current = datetime.now(UTC).strftime("%H:%M")
        if self.last_maintenance_day != today and current >= self.settings.backup_time_utc:
            from src.services.maintenance_service import backup_database

            await backup_database(self.db, self.settings)
            self.last_maintenance_day = today
            async with self.db.engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    async def prune(self, batch_size: int = 500) -> int:
        now = utc_now()
        specs = [
            (
                "events_outbox",
                "status IN ('delivered','not_required')",
                "created_at",
                self.settings.event_retention_days,
            ),
            (
                "events_outbox",
                "status='failed'",
                "created_at",
                self.settings.failed_event_retention_days,
            ),
            (
                "task_events",
                "task_id IN (SELECT id FROM tasks WHERE status IN ('completed','failed','cancelled'))",
                "created_at",
                self.settings.task_event_retention_days,
            ),
            ("audit_logs", "1=1", "created_at", self.settings.audit_retention_days),
            ("sessions", "1=1", "COALESCE(revoked_at,idle_expires_at)", 7),
            (
                "github_deliveries",
                "1=1",
                "received_at",
                self.settings.github_delivery_retention_days,
            ),
        ]
        deleted = 0
        for table, filter_sql, timestamp_column, days in specs:
            cutoff = utc_after(-days * 86400, now)
            while True:
                async with self.db.write_tx() as tx:
                    result = await tx.execute(
                        text(f"""DELETE FROM {table} WHERE rowid IN (
                               SELECT rowid FROM {table} WHERE ({filter_sql}) AND {timestamp_column}<:cutoff LIMIT :limit)"""),
                        {"cutoff": cutoff, "limit": min(batch_size, 500)},
                    )
                    count = result.rowcount or 0
                deleted += count
                if count < batch_size:
                    break
                await asyncio.sleep(0.01)
        return deleted


maintenance = Maintenance()
