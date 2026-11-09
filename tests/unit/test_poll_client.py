from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "poll_client.py"
_spec = importlib.util.spec_from_file_location("poll_client", _SCRIPT)
poll_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poll_client)

ARGS = {"task_id": "task-1", "lease_token": "token", "result_summary": "done", "artifact_ids": []}


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _accepted():
    return SimpleNamespace(
        isError=False, content=[], structuredContent={"task": {"status": "completed"}}
    )


def _rejected():
    text = (
        '{"error":{"code":"validation_error","message":"Artifact does not belong to this task."}}'
    )
    return SimpleNamespace(
        isError=True, content=[SimpleNamespace(text=text)], structuredContent=None
    )


@pytest.mark.asyncio
async def test_complete_retries_a_lost_reply_and_succeeds():
    session = FakeSession([ConnectionError("reset"), _accepted()])

    payload = await poll_client._complete(session, ARGS, backoff=0)

    assert session.calls == 2
    assert payload == {"task": {"status": "completed"}}


@pytest.mark.asyncio
async def test_complete_gives_up_after_the_last_attempt():
    session = FakeSession([ConnectionError(), ConnectionError(), ConnectionError()])

    with pytest.raises(ConnectionError):
        await poll_client._complete(session, ARGS, backoff=0)
    assert session.calls == 3


@pytest.mark.asyncio
async def test_server_rejection_is_raised_not_ignored():
    session = FakeSession([_rejected()])

    with pytest.raises(RuntimeError, match="validation_error"):
        await poll_client._complete(session, ARGS, backoff=0)
    assert session.calls == 1
