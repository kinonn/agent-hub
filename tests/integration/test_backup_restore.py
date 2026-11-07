from __future__ import annotations

import sqlite3

import pytest

from src.config import Settings
from src.services.maintenance_service import backup_database, restore_database
from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_live_wal_backup_and_restore_integrity(harness, tmp_path):
    project, _agent, principal = await create_project_agent(
        harness, "backup-project", "backup-agent"
    )
    await harness["tasks"].create_task(principal, project["slug"], "research", "Backed up task")
    backup_settings = Settings(
        app_env="test",
        database_url=harness["settings"].database_url,
        backup_dir=str(tmp_path / "backups"),
        backup_retention_days=14,
        app_secret_key_file=str(tmp_path / "unused-secret"),
    )
    archive = await backup_database(harness["db"], backup_settings)
    assert archive.is_file() and archive.suffix == ".gz"

    restored_path = tmp_path / "restored" / "agenthub.db"
    restore_settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{restored_path}",
        backup_dir=str(tmp_path / "restored-backups"),
        app_secret_key_file=str(tmp_path / "unused-secret"),
    )
    await restore_database(archive, restore_settings)
    with sqlite3.connect(restored_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001_initial"
        )
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
