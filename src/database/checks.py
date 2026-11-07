"""Raspberry Pi startup checks required before serving traffic."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db

logger = logging.getLogger("agenthub.startup")
EXPECTED_REVISION = "0001_initial"
UNSAFE_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb3", "sshfs"}


def _filesystem_type(path: Path) -> str | None:
    try:
        target = path.resolve()
        best_mount = Path("/")
        best_type = None
        for line in Path("/proc/mounts").read_text().splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            mountpoint = Path(fields[1].replace("\\040", " "))
            try:
                target.relative_to(mountpoint)
            except ValueError:
                continue
            if len(str(mountpoint)) >= len(str(best_mount)):
                best_mount, best_type = mountpoint, fields[2]
        return best_type
    except OSError:
        return None


def check_database_filesystem(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    fs_type = _filesystem_type(settings.database_path.parent)
    unsafe = fs_type in UNSAFE_FILESYSTEMS or (fs_type or "").startswith("fuse.")
    if unsafe and not settings.allow_unsafe_db_fs:
        raise RuntimeError(
            f"SQLite database is on unsupported filesystem {fs_type!r}; use a local Docker volume"
        )


async def check_startup(database: Database | None = None, settings: Settings | None = None) -> None:
    database = database or default_db
    settings = settings or get_settings()
    check_database_filesystem(settings)

    async with database.engine.connect() as conn:
        version = (await conn.execute(text("SELECT sqlite_version()"))).scalar_one()
        if tuple(int(part) for part in version.split(".")[:3]) < (3, 35, 0):
            raise RuntimeError(f"SQLite 3.35 or later is required; found {version}")
        if not (await conn.execute(text("SELECT json_valid('{}')"))).scalar_one():
            raise RuntimeError("SQLite JSON1 support is required")
        await conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.__agenthub_fts_check USING fts5(value)"
        )
        await conn.exec_driver_sql("DROP TABLE temp.__agenthub_fts_check")
        foreign_keys = int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
        busy_timeout = int((await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar_one())
        if foreign_keys != 1 or busy_timeout < 5000:
            raise RuntimeError("SQLite foreign_keys and busy_timeout settings are required")
        journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one().lower()
        if journal_mode != "wal":
            raise RuntimeError(f"SQLite WAL mode is required; found {journal_mode}")
        try:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        except Exception as exc:
            raise RuntimeError("Alembic migrations have not run") from exc
        if revision != EXPECTED_REVISION:
            raise RuntimeError(
                f"Database schema revision {revision!r} does not match code {EXPECTED_REVISION!r}"
            )
    if not settings.cookie_secure:
        logger.warning(
            "PLAIN HTTP MODE: browser cookies and bearer tokens are unencrypted on this LAN"
        )
    if not Path(settings.app_secret_key_file).is_file() and settings.app_env == "production":
        raise RuntimeError("APP_SECRET_KEY_FILE must refer to a mounted secret file in production")
    if len(settings.app_secret) < 32:
        raise RuntimeError("APP_SECRET_KEY_FILE must contain at least 32 random bytes")
