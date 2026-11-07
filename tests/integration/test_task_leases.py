from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.database.types import utc_after
from src.errors import AgentHubError
from src.security.tokens import Principal
from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_fencing_artifact_guard_and_terminal_replay(harness):
    project, _agent, principal = await create_project_agent(harness)
    created = await harness["tasks"].create_task(
        principal,
        project["slug"],
        "research",
        "Investigate lease generations",
    )
    task_id = created["task"]["id"]
    first = await harness["tasks"].claim_task(
        principal, project["slug"], task_id, "same-instance-label"
    )
    token_a = first["task"]["lease"]["lease_token"]
    with pytest.raises(AgentHubError) as duplicate_claim:
        await harness["tasks"].claim_task(principal, project["slug"], task_id)
    assert duplicate_claim.value.code == "task_not_claimable"

    with pytest.raises(IntegrityError):
        async with harness["db"].write_tx() as tx:
            await tx.execute(
                text("UPDATE tasks SET status='pending' WHERE id=:id"), {"id": task_id}
            )

    # Expire the lease without reaping, then reap and claim it as the same identity.
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET lease_expires_at=:expiry WHERE id=:id"),
            {"expiry": utc_after(-1), "id": task_id},
        )
    assert (await harness["tasks"].heartbeat_task(principal, task_id, token_a))["lease_expires_at"]
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET lease_expires_at=:expiry WHERE id=:id"),
            {"expiry": utc_after(-1), "id": task_id},
        )
    assert await harness["tasks"].reap_expired() == 1
    # The reaper schedules its normal backoff; simulate that interval passing.
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET available_at=:now WHERE id=:id"),
            {"now": utc_after(-1), "id": task_id},
        )
    second = await harness["tasks"].claim_task(
        principal, project["slug"], task_id, "same-instance-label"
    )
    token_b = second["task"]["lease"]["lease_token"]
    assert token_a != token_b
    with pytest.raises(AgentHubError) as invalid_transition_error:
        await harness["tasks"].update_task(
            principal, task_id, lease_token=token_b, status="cancelled"
        )
    assert invalid_transition_error.value.code == "invalid_transition"

    with pytest.raises(AgentHubError) as heartbeat_error:
        await harness["tasks"].heartbeat_task(principal, task_id, token_a)
    assert heartbeat_error.value.code == "not_lease_owner"
    with pytest.raises(AgentHubError) as update_error:
        await harness["tasks"].update_task(
            principal,
            task_id,
            lease_token=token_a,
            status="blocked",
            blocked_reason="stale executor",
        )
    assert update_error.value.code == "not_lease_owner"
    with pytest.raises(AgentHubError) as complete_error:
        await harness["tasks"].complete_task(principal, task_id, token_a, "stale result")
    assert complete_error.value.code == "not_lease_owner"
    with pytest.raises(AgentHubError) as artifact_error:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "Stale research",
            "This must not publish.",
            task_id=task_id,
            lease_token=token_a,
        )
    assert artifact_error.value.code == "not_lease_owner"

    # Raw tokens are returned once; only hashes survive in operational state.
    async with harness["db"].engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT lease_token_hash FROM tasks WHERE id=:id"), {"id": task_id}
            )
        ).scalar_one()
    assert token_a not in stored and token_b not in stored

    result = await harness["tasks"].complete_task(principal, task_id, token_b, "current result")
    async with harness["db"].engine.connect() as conn:
        event_count = (
            await conn.execute(
                text("SELECT count(*) FROM task_events WHERE task_id=:id"), {"id": task_id}
            )
        ).scalar_one()
        outbox_count = (
            await conn.execute(
                text("SELECT count(*) FROM events_outbox WHERE aggregate_id=:id"), {"id": task_id}
            )
        ).scalar_one()
        audit_count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE resource_type='task' AND resource_id=:id"
                ),
                {"id": task_id},
            )
        ).scalar_one()
    replay = await harness["tasks"].complete_task(principal, task_id, token_b, "current result")
    assert result["task"]["result_summary"] == "current result"
    assert replay["idempotent_replay"] is True
    with pytest.raises(AgentHubError) as conflict:
        await harness["tasks"].complete_task(principal, task_id, token_b, "conflicting result")
    assert conflict.value.code == "completion_conflict"
    with pytest.raises(AgentHubError) as no_lease:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "After completion",
            "Not leased.",
            task_id=task_id,
            lease_token=token_b,
        )
    assert no_lease.value.code == "task_not_leased"
    async with harness["db"].engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM task_events WHERE task_id=:id"), {"id": task_id}
            )
        ).scalar_one() == event_count
        assert (
            await conn.execute(
                text("SELECT count(*) FROM events_outbox WHERE aggregate_id=:id"), {"id": task_id}
            )
        ).scalar_one() == outbox_count
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE resource_type='task' AND resource_id=:id"
                ),
                {"id": task_id},
            )
        ).scalar_one() == audit_count


@pytest.mark.asyncio
async def test_expired_unassigned_task_can_be_claimed_by_different_agent(harness):
    project, _first_agent, first_principal = await create_project_agent(
        harness, "reassignment", "researcher-a"
    )
    second = await harness["auth"].create_agent("researcher-b", "other", capabilities=["research"])
    await harness["auth"].set_agent_membership(project["id"], second["agent"]["id"], "contributor")
    second_principal = Principal(kind="agent", id=second["agent"]["id"], name="researcher-b")
    created = await harness["tasks"].create_task(
        first_principal, project["slug"], "research", "Unassigned task"
    )
    task_id = created["task"]["id"]
    await harness["tasks"].claim_task(first_principal, project["slug"], task_id)
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET lease_expires_at=:expiry WHERE id=:id"),
            {"expiry": utc_after(-1), "id": task_id},
        )
    assert await harness["tasks"].reap_expired() == 1
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET available_at=:now WHERE id=:id"),
            {"now": utc_after(-1), "id": task_id},
        )
    reclaimed = await harness["tasks"].claim_task(second_principal, project["slug"], task_id)
    assert reclaimed["task"]["lease_owner_agent_id"] == second_principal.id
    assert reclaimed["task"]["assigned_agent_id"] is None


@pytest.mark.asyncio
async def test_retryable_failure_is_not_replayable_and_maintainer_retry_clears_final(harness):
    project, _agent, principal = await create_project_agent(harness, "retry-project", "retry-agent")
    maintainer = await harness["auth"].create_agent("maintainer", "other")
    await harness["auth"].set_agent_membership(
        project["id"], maintainer["agent"]["id"], "maintainer"
    )
    maintainer_principal = Principal(kind="agent", id=maintainer["agent"]["id"], name="maintainer")
    created = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Will fail", max_attempts=1
    )
    claim = await harness["tasks"].claim_task(principal, project["slug"], created["task"]["id"])
    token = claim["task"]["lease"]["lease_token"]
    failed = await harness["tasks"].update_task(
        principal,
        created["task"]["id"],
        lease_token=token,
        status="failed",
        error_detail="permanent",
        retryable=False,
    )
    assert failed["task"]["status"] == "failed"
    replay = await harness["tasks"].update_task(
        principal,
        created["task"]["id"],
        lease_token=token,
        status="failed",
        error_detail="permanent",
        retryable=False,
    )
    assert replay["idempotent_replay"] is True
    await harness["tasks"].retry_task(maintainer_principal, created["task"]["id"])
    with pytest.raises(AgentHubError) as old_token:
        await harness["tasks"].update_task(
            principal,
            created["task"]["id"],
            lease_token=token,
            status="failed",
            error_detail="permanent",
            retryable=False,
        )
    assert old_token.value.code == "not_lease_owner"


@pytest.mark.asyncio
async def test_update_and_complete_terminal_error_codes_are_stable(harness):
    project, _agent, principal = await create_project_agent(
        harness, "terminal-codes", "terminal-agent"
    )
    task = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Terminal report"
    )
    claimed = await harness["tasks"].claim_task(principal, project["slug"], task["task"]["id"])
    task_id = task["task"]["id"]
    token = claimed["task"]["lease"]["lease_token"]
    await harness["tasks"].update_task(
        principal,
        task_id,
        lease_token=token,
        status="failed",
        error_detail="first report",
        retryable=False,
    )

    with pytest.raises(AgentHubError) as update_conflict:
        await harness["tasks"].update_task(
            principal,
            task_id,
            lease_token=token,
            status="failed",
            error_detail="different report",
            retryable=False,
        )
    assert update_conflict.value.code == "completion_conflict"

    with pytest.raises(AgentHubError) as complete_conflict:
        await harness["tasks"].complete_task(principal, task_id, token, "different terminal kind")
    assert complete_conflict.value.code == "invalid_transition"


@pytest.mark.asyncio
async def test_dashboard_task_transition_audit_records_user_actor(harness):
    project, _agent, agent = await create_project_agent(harness, "audit-attribution", "audit-agent")
    user = await harness["auth"].create_user(
        "maintainer@example.com", "a-long-test-password", "Maintainer", "member"
    )
    await harness["auth"].set_user_membership(project["id"], user["id"], "maintainer")
    user_principal = Principal(kind="user", id=user["id"], name=user["display_name"], role="member")
    task = await harness["tasks"].create_task(
        agent, project["slug"], "research", "Cancel from dashboard"
    )
    await harness["tasks"].claim_task(agent, project["slug"], task["task"]["id"])

    await harness["tasks"].cancel_task(user_principal, task["task"]["id"])
    async with harness["db"].engine.connect() as conn:
        audit = (
            (
                await conn.execute(
                    text("""SELECT actor_type,actor_id,project_id FROM audit_logs
                           WHERE action='task.cancel_requested' AND resource_id=:task"""),
                    {"task": task["task"]["id"]},
                )
            )
            .mappings()
            .one()
        )
    assert audit["actor_type"] == "user"
    assert audit["actor_id"] == user["id"]
    assert audit["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_task_idempotency_replay_uses_stable_request_fingerprint(harness):
    project, _agent, principal = await create_project_agent(
        harness, "idempotency-project", "idempotent-agent"
    )
    first = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Same request", idempotency_key="request-1"
    )
    replay = await harness["tasks"].create_task(
        principal, project["id"], "research", "Same request", idempotency_key="request-1"
    )
    assert replay["idempotent_replay"] is True
    assert replay["task"]["id"] == first["task"]["id"]
    with pytest.raises(AgentHubError) as conflict:
        await harness["tasks"].create_task(
            principal, project["slug"], "research", "Changed request", idempotency_key="request-1"
        )
    assert conflict.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_reaper_fails_exhausted_attempts_and_honors_cancel_request(harness):
    project, _agent, principal = await create_project_agent(
        harness, "reaper-project", "reaper-agent"
    )
    cancel_task = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Cancel at expiry", max_attempts=1
    )
    fail_task = await harness["tasks"].create_task(
        principal, project["slug"], "research", "Fail at expiry", max_attempts=1
    )
    await harness["tasks"].claim_task(principal, project["slug"], cancel_task["task"]["id"])
    await harness["tasks"].claim_task(principal, project["slug"], fail_task["task"]["id"])
    await harness["tasks"].cancel_task(principal, cancel_task["task"]["id"])
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("UPDATE tasks SET lease_expires_at=:expiry WHERE id IN (:cancel,:fail)"),
            {
                "expiry": utc_after(-1),
                "cancel": cancel_task["task"]["id"],
                "fail": fail_task["task"]["id"],
            },
        )
    assert await harness["tasks"].reap_expired() == 2
    cancelled = (await harness["tasks"].get_task(principal, cancel_task["task"]["id"]))["task"]
    failed = (await harness["tasks"].get_task(principal, fail_task["task"]["id"]))["task"]
    assert cancelled["status"] == "cancelled"
    assert failed["status"] == "failed"
    assert failed["error_detail"] == "lease expired; attempts exhausted"
