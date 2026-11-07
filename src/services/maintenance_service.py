"""WAL-safe SQLite backup, restore, and integrity checks."""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db


def _backup_sync(source_path: Path, backup_dir: Path, retention_days: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = backup_dir / f"agenthub-{stamp}.sqlite"
    compressed = backup_dir / f"agenthub-{stamp}.sqlite.gz"
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=5)
    target = sqlite3.connect(raw_path)
    raw_path.chmod(0o600)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    except Exception:
        target.close()
        source.close()
        raw_path.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    raw_path.chmod(0o600)
    with (
        raw_path.open("rb") as input_file,
        gzip.open(compressed, "wb", compresslevel=6) as output_file,
    ):
        shutil.copyfileobj(input_file, output_file)
    compressed.chmod(0o600)
    raw_path.unlink()
    _prune_backups(backup_dir, retention_days)
    return compressed


def _prune_backups(backup_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
    files = sorted(
        backup_dir.glob("agenthub-*.sqlite.gz"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for index, path in enumerate(files):
        if index >= retention_days or path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


async def backup_database(
    database: Database | None = None, settings: Settings | None = None
) -> Path:
    database = database or default_db
    settings = settings or get_settings()
    return await asyncio.to_thread(
        _backup_sync,
        settings.database_path,
        Path(settings.backup_dir),
        settings.backup_retention_days,
    )


def _restore_sync(
    source_path: Path, database_path: Path, expected_revision: str = "0001_initial"
) -> None:
    import tempfile

    database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="agenthub-restore-", suffix=".sqlite", dir=database_path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        if source_path.suffix == ".gz":
            with gzip.open(source_path, "rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
        else:
            shutil.copyfile(source_path, temporary)
        _verify_restore(temporary, expected_revision)

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        current_files = [
            database_path,
            Path(str(database_path) + "-wal"),
            Path(str(database_path) + "-shm"),
        ]
        moved: list[tuple[Path, Path]] = []
        try:
            for current in current_files:
                if current.exists():
                    preserved = current.with_name(f"{current.name}.pre-restore-{stamp}")
                    os.replace(current, preserved)
                    moved.append((current, preserved))
            os.replace(temporary, database_path)
        except Exception:
            for current, preserved in reversed(moved):
                if preserved.exists():
                    os.replace(preserved, current)
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _verify_restore(path: Path, expected_revision: str) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        try:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("Backup does not contain an Alembic version") from exc
        if not revision or revision[0] != expected_revision:
            raise RuntimeError(
                f"Backup schema {revision[0] if revision else 'unknown'} is incompatible"
            )
    finally:
        connection.close()


async def restore_database(source_path: Path, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    await asyncio.to_thread(_restore_sync, source_path, settings.database_path)


def verify_integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
