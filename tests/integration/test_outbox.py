from __future__ import annotations

import pytest
from sqlalchemy import text

from src.database.types import utc_after
from src.workers.outbox_dispatcher import OutboxDispatcher
from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_stuck_processing_event_is_recovered_and_retried(harness):
    project, _agent, principal = await create_project_agent(harness, "outbox-project", "poll-agent")
    adapter = await harness["auth"].create_agent("adapter-agent", "other", execution_mode="adapter")
    await harness["auth"].set_agent_membership(project["id"], adapter["agent"]["id"], "contributor")
    task = await harness["tasks"].create_task(principal, project["slug"], "research", "Wakeup task")
    task_id = task["task"]["id"]
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("""UPDATE events_outbox SET status='processing',processing_started_at=:old,
                       attempt_count=2 WHERE aggregate_id=:aggregate AND event_type='task.available'"""),
            {
                "old": utc_after(-harness["settings"].outbox_processing_timeout_seconds - 1),
                "aggregate": task_id,
            },
        )

    dispatcher = OutboxDispatcher(harness["db"], harness["settings"])
    assert await dispatcher.dispatch_batch() == 1
    async with harness["db"].engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,processing_started_at,attempt_count,last_error FROM events_outbox WHERE aggregate_id=:id AND event_type='task.available'"
                    ),
                    {"id": task_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "pending"
    assert row["processing_started_at"] is None
    assert row["attempt_count"] == 3
    assert row["last_error"]
