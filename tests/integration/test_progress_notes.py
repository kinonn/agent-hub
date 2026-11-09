from __future__ import annotations

import pytest

from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_progress_notes_from_update_and_heartbeat_are_recorded(harness):
    project, _agent, principal = await create_project_agent(harness)
    created = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Investigate progress"
    )
    task_id = created["task"]["id"]
    claimed = await harness["tasks"].claim_task(principal, project["slug"], task_id, "instance")
    token = claimed["task"]["lease"]["lease_token"]

    await harness["tasks"].update_task(
        principal, task_id, lease_token=token, progress_note="reading sources"
    )
    await harness["tasks"].heartbeat_task(principal, task_id, token, "drafting summary")
    await harness["tasks"].heartbeat_task(principal, task_id, token)

    events = await harness["tasks"].task_events(principal, task_id)
    notes = sorted(e["payload"]["note"] for e in events if e["event_type"] == "progress")
    assert notes == ["drafting summary", "reading sources"]
    assert [e["event_type"] for e in events].count("heartbeat") == 2
