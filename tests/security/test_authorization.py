from __future__ import annotations

import pytest

from src.errors import AgentHubError
from src.security.tokens import Principal
from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_reader_and_nonmember_permissions(harness):
    project, _agent, contributor = await create_project_agent(harness, "permissions", "contributor")
    reader = await harness["auth"].create_agent("reader", "other")
    await harness["auth"].set_agent_membership(project["id"], reader["agent"]["id"], "reader")
    reader_principal = Principal(kind="agent", id=reader["agent"]["id"], name="reader")
    outside = await harness["auth"].create_agent("outside", "other")
    outside_principal = Principal(kind="agent", id=outside["agent"]["id"], name="outside")

    created = await harness["tasks"].create_task(
        contributor, project["slug"], "research", "Reader-visible task"
    )
    task_id = created["task"]["id"]
    assert (await harness["tasks"].get_task(reader_principal, task_id))["task"]["id"] == task_id
    assert (
        await harness["tasks"].get_pending_tasks(
            reader_principal, project["slug"], claimable_only=True
        )
    )["tasks"] == []

    with pytest.raises(AgentHubError) as create_error:
        await harness["tasks"].create_task(
            reader_principal, project["slug"], "research", "Forbidden"
        )
    assert create_error.value.code == "insufficient_role"
    with pytest.raises(AgentHubError) as claim_error:
        await harness["tasks"].claim_task(reader_principal, project["slug"])
    assert claim_error.value.code == "insufficient_role"
    with pytest.raises(AgentHubError) as message_error:
        await harness["messages"].publish_message(
            reader_principal, project["slug"], "s", "b", "info"
        )
    assert message_error.value.code == "insufficient_role"

    for operation in (
        harness["tasks"].get_task(outside_principal, task_id),
        harness["messages"].get_messages(outside_principal, project["slug"]),
        harness["artifacts"].search_knowledge(outside_principal, project["slug"], "research"),
    ):
        with pytest.raises(AgentHubError) as denied:
            await operation
        assert denied.value.code == "not_found"


@pytest.mark.asyncio
async def test_lease_token_requires_owner_and_current_generation(harness):
    project, _agent, owner = await create_project_agent(harness, "owner-check", "lease-owner")
    other = await harness["auth"].create_agent("not-owner", "other")
    await harness["auth"].set_agent_membership(project["id"], other["agent"]["id"], "contributor")
    wrong_agent = Principal(kind="agent", id=other["agent"]["id"], name="not-owner")
    created = await harness["tasks"].create_task(owner, project["slug"], "research", "Fenced")
    claim = await harness["tasks"].claim_task(owner, project["slug"], created["task"]["id"])
    token = claim["task"]["lease"]["lease_token"]
    for principal, supplied in ((wrong_agent, token), (owner, "wrong-token"), (owner, "")):
        with pytest.raises(AgentHubError) as rejected:
            await harness["tasks"].heartbeat_task(principal, created["task"]["id"], supplied)
        assert rejected.value.code == "not_lease_owner"
        assert "lease_owner" not in rejected.value.message
        assert "result_summary" not in rejected.value.message
