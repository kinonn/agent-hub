"""Async SQLAlchemy/aiosqlite connection handling and serialized writes."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.config import Settings, get_settings


class Database:
    def __init__(self, url: str | None = None, *, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.url = url or self.settings.database_url
        connect_args: dict[str, object] = {"timeout": 5.0}
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            connect_args=connect_args,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        event.listen(self.engine.sync_engine, "connect", self._configure_connection)

    @staticmethod
    def _configure_connection(dbapi_connection, _connection_record) -> None:
        # SQLAlchemy owns connection setup; explicit BEGIN IMMEDIATE is issued by write_tx.
        with suppress(Exception):
            dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA cache_size = -16000")
        cursor.close()

    async def initialize(self) -> None:
        path = self.settings.database_path
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.connect() as conn:
            mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one().lower()
            if mode != "wal":
                await conn.exec_driver_sql("PRAGMA journal_mode = WAL")
                mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one().lower()
            if mode != "wal":
                raise RuntimeError(f"SQLite WAL mode is required; got {mode!r}")

    @asynccontextmanager
    async def write_tx(self) -> AsyncIterator[AsyncConnection]:
        """Begin an immediate transaction; retry only lock acquisition, never work."""
        for attempt in range(5):
            conn = await self.engine.connect()
            transaction = await conn.begin()
            try:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as exc:
                await transaction.rollback()
                await conn.close()
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt == 4:
                    raise
                delay = 0.05 * (2**attempt) + random.uniform(0, 0.05)
                await asyncio.sleep(delay)
                continue
            try:
                yield conn
            except BaseException:
                with suppress(Exception):
                    await transaction.rollback()
                raise
            else:
                await transaction.commit()
            finally:
                await conn.close()
            return
        raise RuntimeError("unreachable write retry loop")

    async def fetch_one(self, sql: str, params: dict | None = None) -> dict | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def fetch_all(self, sql: str, params: dict | None = None) -> list[dict]:
        async with self.engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings().all()]

    async def execute_read(self, sql: str, params: dict | None = None):
        async with self.engine.connect() as conn:
            return await conn.execute(text(sql), params or {})

    async def close(self) -> None:
        await self.engine.dispose()


db = Database()
