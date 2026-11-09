from __future__ import annotations

import pytest
from sqlalchemy import text

from src.errors import AgentHubError
from src.security.tokens import Principal

PASSWORD = "correct-horse-battery"


async def _sole_admin(harness):
    admin = await harness["auth"].create_first_admin("admin@example.com", PASSWORD)
    return admin, Principal(kind="user", id=admin["id"], name="Admin", role="admin")


@pytest.mark.asyncio
async def test_sole_admin_cannot_deactivate_or_demote_self(harness):
    admin, actor = await _sole_admin(harness)

    with pytest.raises(AgentHubError) as deactivate:
        await harness["auth"].patch_user(admin["id"], {"is_active": False}, actor)
    assert deactivate.value.code == "validation_error"
    with pytest.raises(AgentHubError) as demote:
        await harness["auth"].patch_user(admin["id"], {"role": "member"}, actor)
    assert demote.value.code == "validation_error"

    login = await harness["auth"].login("admin@example.com", PASSWORD, "127.0.0.1", "test")
    assert login["user"]["role"] == "admin"


@pytest.mark.asyncio
async def test_admin_can_step_down_while_another_admin_remains(harness):
    admin, actor = await _sole_admin(harness)
    second = await harness["auth"].create_user(
        "second@example.com", PASSWORD, "Second", "admin", actor=actor
    )

    stepped_down = await harness["auth"].patch_user(admin["id"], {"role": "member"}, actor)
    assert stepped_down["role"] == "member"

    second_actor = Principal(kind="user", id=second["id"], name="Second", role="admin")
    with pytest.raises(AgentHubError):
        await harness["auth"].patch_user(second["id"], {"is_active": False}, second_actor)


@pytest.mark.asyncio
async def test_members_can_still_be_deactivated(harness):
    _admin, actor = await _sole_admin(harness)
    member = await harness["auth"].create_user(
        "member@example.com", PASSWORD, "Member", actor=actor
    )

    result = await harness["auth"].patch_user(member["id"], {"is_active": False}, actor)
    assert result["is_active"] is False


@pytest.mark.asyncio
async def test_create_first_admin_only_counts_active_admins(harness):
    admin, _actor = await _sole_admin(harness)
    with pytest.raises(AgentHubError) as refused:
        await harness["auth"].create_first_admin("other@example.com", PASSWORD)
    assert refused.value.code == "forbidden"

    # Simulates a database left with only an inactive admin, e.g. by an earlier version.
    async with harness["db"].write_tx() as tx:
        await tx.execute(text("UPDATE users SET is_active=0 WHERE id=:id"), {"id": admin["id"]})
    recovered = await harness["auth"].create_first_admin("rescue@example.com", PASSWORD)
    assert recovered["role"] == "admin"
