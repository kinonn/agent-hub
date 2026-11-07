from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from src.services.artifact_service import ArtifactService
from src.services.github_service import GitHubClient, GitHubError
from tests.conftest import create_project_agent


class FakeGitHub:
    def __init__(self, responses, file_error: Exception | None = None):
        self.responses = list(responses)
        self.etags: list[str | None] = []
        self.file_error = file_error

    async def get_pull_request_conditional(self, _artifact, etag=None):
        self.etags.append(etag)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def fetch_file(self, _artifact, _ref):
        if self.file_error:
            raise self.file_error
        raise AssertionError("An open pull request must not fetch its file")


async def _create_open_pr_artifact(harness, suffix: str = ""):
    project, _agent, principal = await create_project_agent(
        harness, f"reconcile-{suffix or 'project'}", f"reconcile-agent-{suffix or 'default'}"
    )
    artifact = await harness["artifacts"].publish_artifact(
        principal,
        project["slug"],
        "research",
        "Conditional request sample",
        "Published artifact body",
    )
    artifact_id = artifact["artifact"]["id"]
    async with harness["db"].write_tx() as tx:
        await tx.execute(
            text("""UPDATE artifacts SET publication_status='in_review',pull_request_number=:pr,
                       pull_request_url='https://github.com/example-owner/agenthub-knowledge/pull/42'
                       WHERE id=:id"""),
            {"pr": 42, "id": artifact_id},
        )
    return artifact_id


@pytest.mark.asyncio
async def test_reconciler_uses_etag_and_skips_unchanged_pull_requests(harness):
    artifact_id = await _create_open_pr_artifact(harness)
    github = FakeGitHub([({"state": "open"}, '"etag-1"', False), (None, '"etag-1"', True)])
    service = ArtifactService(harness["db"], harness["settings"], github)

    assert await service.reconcile_open() == 0
    assert await service.reconcile_open() == 0
    assert github.etags == [None, '"etag-1"']
    assert 42 in service._reconcile_etags
    async with harness["db"].engine.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT publication_status FROM artifacts WHERE id=:id"), {"id": artifact_id}
            )
        ).scalar_one()
    assert status == "in_review"


@pytest.mark.asyncio
async def test_reconciler_backs_off_after_github_rate_limit(harness):
    await _create_open_pr_artifact(harness, "backoff")
    github = FakeGitHub([GitHubError(403, "rate limit", retry_after=900)])
    service = ArtifactService(harness["db"], harness["settings"], github)

    assert await service.reconcile_open() == 0
    assert await service.reconcile_open() == 0
    assert github.etags == [None]
    assert service._reconcile_failures[42] == 1


@pytest.mark.asyncio
async def test_reconciler_does_not_cache_etag_before_merge_file_fetch_succeeds(harness):
    await _create_open_pr_artifact(harness, "merge-fetch")
    github = FakeGitHub(
        [
            (
                {"state": "closed", "merged_at": "now", "merge_commit_sha": "merge-sha"},
                '"merged-etag"',
                False,
            )
        ],
        file_error=GitHubError(503, "temporary file fetch error"),
    )
    service = ArtifactService(harness["db"], harness["settings"], github)

    assert await service.reconcile_open() == 0
    assert 42 not in service._reconcile_etags
    assert service._reconcile_retry_at[42] > 0


@pytest.mark.asyncio
async def test_github_conditional_pull_request_sends_if_none_match(harness, monkeypatch):
    client = GitHubClient(harness["settings"])
    request = httpx.Request("GET", "https://api.github.com/repos/example/repo/pulls/1")
    response = httpx.Response(304, headers={"ETag": '"etag-2"'}, request=request)
    captured = {}

    async def fake_send(_method, _path, **kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(client, "_send", fake_send)
    pull, etag, not_modified = await client.get_pull_request_conditional(
        {"repository_owner": "example", "repository_name": "repo", "pull_request_number": 1},
        '"etag-1"',
    )
    assert pull is None
    assert etag == '"etag-2"'
    assert not_modified is True
    assert captured["headers"] == {"If-None-Match": '"etag-1"'}
