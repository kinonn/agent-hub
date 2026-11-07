from __future__ import annotations

import asyncio
import multiprocessing

import pytest

from src.security.tokens import Principal
from tests.conftest import create_project_agent


def _claim_until_empty(database_url: str, project: str, agent_id: str, output) -> None:
    async def claim():
        from src.config import Settings
        from src.database.connection import Database
        from src.services.task_service import TaskService

        settings = Settings(database_url=database_url, app_env="test")
        database = Database(database_url, settings=settings)
        service = TaskService(database, settings)
        principal = Principal(kind="agent", id=agent_id, name="same-agent-identity")
        claimed = []
        try:
            while True:
                result = await service.claim_task(principal, project)
                if result is None:
                    break
                claimed.append(result["task"]["id"])
        finally:
            await database.close()
        return claimed

    try:
        output.put(("ok", asyncio.run(claim())))
    except BaseException as exc:
        output.put(("error", type(exc).__name__))


@pytest.mark.asyncio
async def test_200_claims_across_two_processes_are_unique(harness):
    project, _agent, principal = await create_project_agent(
        harness, "concurrent-claims", "claim-agent"
    )
    for index in range(200):
        await harness["tasks"].create_task(
            principal,
            project["slug"],
            "research",
            f"Concurrent task {index:03d}",
        )

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    database_url = harness["settings"].database_url
    processes = [
        context.Process(
            target=_claim_until_empty, args=(database_url, project["slug"], principal.id, queue)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    assert all(kind == "ok" for kind, _ in results), results
    claimed = [task_id for _, task_ids in results for task_id in task_ids]
    assert len(claimed) == 200
    assert len(set(claimed)) == 200
    assert len(results[0][1]) + len(results[1][1]) == 200
