from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from src.config import Settings, get_settings
from src.database.connection import Database
from src.security.tokens import Principal
from src.services.artifact_service import ArtifactService
from src.services.auth_service import AuthService
from src.services.message_service import MessageService
from src.services.task_service import TaskService


@pytest.fixture
def database_config(tmp_path: Path, monkeypatch):
    database_file = tmp_path / "agenthub.db"
    database_url = f"sqlite+aiosqlite:///{database_file}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    settings = Settings(
        app_env="test",
        database_url=database_url,
        app_secret_key_file=str(tmp_path / "unused-secret"),
        github_repository_owner="example-owner",
        github_knowledge_repository="agenthub-knowledge",
        max_artifact_bytes=4096,
        task_lease_seconds=120,
        task_heartbeat_seconds=40,
    )
    database = Database(database_url, settings=settings)
    return {"db": database, "settings": settings, "tmp_path": tmp_path}


@pytest_asyncio.fixture
async def harness(database_config):
    database = database_config["db"]
    settings = database_config["settings"]
    await database.initialize()
    auth = AuthService(database, settings)
    tasks = TaskService(database, settings)
    messages = MessageService(database, settings)
    artifacts = ArtifactService(database, settings)
    result = {
        "db": database,
        "settings": settings,
        "tmp_path": database_config["tmp_path"],
        "auth": auth,
        "tasks": tasks,
        "messages": messages,
        "artifacts": artifacts,
    }
    try:
        yield result
    finally:
        await database.close()
        get_settings.cache_clear()


async def create_project_agent(harness, slug: str = "test-project", name: str = "agent"):
    project = await harness["auth"].create_project(slug, slug.replace("-", " ").title())
    agent = await harness["auth"].create_agent(name, "other", capabilities=["research"])
    await harness["auth"].set_agent_membership(project["id"], agent["agent"]["id"], "contributor")
    principal = Principal(kind="agent", id=agent["agent"]["id"], name=name)
    return project, agent, principal
