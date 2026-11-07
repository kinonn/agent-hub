from __future__ import annotations

import pytest
from sqlalchemy import text

from src.errors import AgentHubError
from src.security.content_policy import slugify_title, validate_artifact
from src.services.github_service import GitHubClient, strip_front_matter
from tests.conftest import create_project_agent


@pytest.mark.asyncio
async def test_message_idempotency_and_lazy_receipt(harness):
    project, agent, principal = await create_project_agent(harness, "message-project", "sender")
    recipient = await harness["auth"].create_agent("recipient", "other")
    await harness["auth"].set_agent_membership(project["id"], recipient["agent"]["id"], "reader")

    first = await harness["messages"].publish_message(
        principal,
        project["slug"],
        "Question",
        "Can you review this?",
        "question",
        recipient_agent_id=recipient["agent"]["id"],
        idempotency_key="question-1",
    )
    replay = await harness["messages"].publish_message(
        principal,
        project["id"],
        "Question",
        "Can you review this?",
        "question",
        recipient_agent_id=recipient["agent"]["id"],
        idempotency_key="question-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["message"]["id"] == first["message"]["id"]
    with pytest.raises(AgentHubError) as conflict:
        await harness["messages"].publish_message(
            principal,
            project["slug"],
            "Changed",
            "Different body",
            "question",
            recipient_agent_id=recipient["agent"]["id"],
            idempotency_key="question-1",
        )
    assert conflict.value.code == "idempotency_conflict"

    unread = await harness["messages"].get_messages(
        type(principal)(kind="agent", id=recipient["agent"]["id"], name="recipient"),
        project["slug"],
        unread_only=True,
    )
    assert len(unread["messages"]) == 1
    receipt = await harness["messages"].mark_message_read(
        type(principal)(kind="agent", id=recipient["agent"]["id"], name="recipient"),
        first["message"]["id"],
        acknowledge=True,
    )
    assert receipt["receipt"]["acknowledged_at"]
    unread = await harness["messages"].get_messages(
        type(principal)(kind="agent", id=recipient["agent"]["id"], name="recipient"),
        project["slug"],
        unread_only=True,
    )
    assert unread["messages"] == []


@pytest.mark.asyncio
async def test_message_cursor_is_stable_and_bound_to_filters(harness):
    project, _agent, principal = await create_project_agent(
        harness, "cursor-project", "cursor-agent"
    )
    ids = []
    for index in range(3):
        result = await harness["messages"].publish_message(
            principal, project["slug"], f"Message {index}", "body", "info"
        )
        ids.append(result["message"]["id"])
    first = await harness["messages"].get_messages(principal, project["slug"], limit=2)
    second = await harness["messages"].get_messages(
        principal, project["slug"], limit=2, cursor=first["next_cursor"]
    )
    paged = [message["id"] for message in first["messages"] + second["messages"]]
    assert paged == ids
    with pytest.raises(AgentHubError) as mismatch:
        await harness["messages"].get_messages(
            principal, project["slug"], limit=2, cursor=first["next_cursor"], unread_only=True
        )
    assert mismatch.value.code == "validation_error"


@pytest.mark.asyncio
async def test_artifact_idempotency_content_policy_versioning_and_search(harness):
    project, _agent, principal = await create_project_agent(
        harness, "knowledge-project", "knowledge-agent"
    )
    content = "SQLite WAL supports readers while a single writer commits durable research."
    first = await harness["artifacts"].publish_artifact(
        principal,
        project["slug"],
        "research",
        "SQLite WAL research",
        content,
        summary="Research on WAL",
        idempotency_key="wal-research-v1",
    )
    replay = await harness["artifacts"].publish_artifact(
        principal,
        project["id"],
        "research",
        "SQLite WAL research",
        content,
        summary="Research on WAL",
        idempotency_key="wal-research-v1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["artifact"]["id"] == first["artifact"]["id"]
    assert (
        first["artifact"]["file_path"]
        == f"projects/{project['slug']}/research/sqlite-wal-research.md"
    )
    with pytest.raises(AgentHubError) as conflict:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "SQLite WAL research",
            "different content",
            summary="Research on WAL",
            idempotency_key="wal-research-v1",
        )
    assert conflict.value.code == "idempotency_conflict"

    with pytest.raises(AgentHubError) as open_version:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "SQLite WAL research",
            content,
            summary="Research on WAL",
            document_key="sqlite-wal-research",
        )
    assert open_version.value.code == "artifact_open_version_exists"

    with pytest.raises(AgentHubError) as secret:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "Unsafe",
            "-----BEGIN PRIVATE KEY-----\nnope",
        )
    assert secret.value.code == "content_policy_violation"
    with pytest.raises(AgentHubError) as path:
        await harness["artifacts"].publish_artifact(
            principal,
            project["slug"],
            "research",
            "Unsafe",
            "body",
            document_key="../escape",
        )
    assert path.value.code == "validation_error"

    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("""UPDATE artifacts SET publication_status='published',commit_sha='abc123',published_at=:now
                   WHERE id=:id"""),
            {"now": first["artifact"]["created_at"], "id": first["artifact"]["id"]},
        )
    assert await harness["artifacts"].reindex() == 1
    result = await harness["artifacts"].search_knowledge(principal, project["slug"], "sqlite WAL")
    assert result["results"][0]["artifact_id"] == first["artifact"]["id"]
    assert result["results"][0]["commit_sha"] == "abc123"

    # FTS control characters/operators are quoted as literal phrases, not executed.
    for literal in ('"sqlite"', "*", "OR", "NEAR(", "title:wal"):
        await harness["artifacts"].search_knowledge(principal, project["slug"], literal)
    with pytest.raises(AgentHubError) as empty_query:
        await harness["artifacts"].search_knowledge(principal, project["slug"], "   ")
    assert empty_query.value.code == "validation_error"


def test_artifact_path_and_front_matter_edge_cases():
    assert slugify_title("Research..Architecture") == "research-architecture"
    assert slugify_title("Example.md") == "example"
    with pytest.raises(AgentHubError) as extension:
        validate_artifact("A report", "Body", "report.md", max_bytes=4096)
    assert extension.value.code == "validation_error"

    fake_front_matter = "\ufeff---\r\ntitle: spoofed\r\n---\r\nBody"
    with pytest.raises(AgentHubError) as front_matter:
        validate_artifact("A report", fake_front_matter, None, max_bytes=4096)
    assert front_matter.value.code == "content_policy_violation"
    assert "spoofed" not in front_matter.value.message

    body = "The body must survive the front-matter round trip.\nSecond line."
    rendered = GitHubClient.render_artifact(
        {
            "id": "artifact-id",
            "project_slug": "sample-project",
            "artifact_type": "research",
            "version": 1,
            "task_id": None,
            "created_at": "2026-01-02T03:04:05.006Z",
            "content": body,
        },
        "sample-agent",
    )
    assert strip_front_matter(rendered) == body
    assert strip_front_matter(rendered.replace("\n", "\r\n")) == body.replace("\n", "\r\n")
