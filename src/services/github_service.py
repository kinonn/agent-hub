"""Minimal GitHub REST client for branch, pull-request, and merge verification."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from src.config import Settings, get_settings
from src.security.content_policy import FRONT_MATTER_RE

GITHUB_API = "https://api.github.com"


@dataclass
class GitHubError(Exception):
    status_code: int
    message: str
    retry_after: int | None = None

    def __str__(self) -> str:
        return f"GitHub API error ({self.status_code}): {self.message}"

    @property
    def retryable(self) -> bool:
        return (
            self.status_code == 429
            or self.status_code >= 500
            or self.status_code in {408, 409}
            or (self.status_code == 403 and self.retry_after is not None)
        )


class GitHubClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._app_token: str | None = None
        self._app_token_expiry = 0.0

    @property
    def configured(self) -> bool:
        if (
            self.settings.github_token_file
            and Path(self.settings.github_token_file).is_file()
            and Path(self.settings.github_token_file).stat().st_size > 0
        ):
            return True
        return bool(
            self.settings.github_app_id
            and self.settings.github_installation_id
            and Path(self.settings.github_private_key_file).is_file()
            and Path(self.settings.github_private_key_file).stat().st_size > 0
        )

    async def _token(self) -> str:
        if self.settings.github_token_file and Path(self.settings.github_token_file).is_file():
            return Path(self.settings.github_token_file).read_text().strip()
        if self._app_token and self._app_token_expiry > time.time() + 60:
            return self._app_token
        if not self.settings.github_app_id or not self.settings.github_installation_id:
            raise GitHubError(401, "GitHub credentials are not configured")
        private_key = Path(self.settings.github_private_key_file).read_text()
        now = int(time.time())
        assertion = jwt.encode(
            {"iat": now - 30, "exp": now + 540, "iss": self.settings.github_app_id},
            private_key,
            algorithm="RS256",
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{GITHUB_API}/app/installations/{self.settings.github_installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {assertion}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            raise self._error(response)
        data = response.json()
        self._app_token = data["token"]
        self._app_token_expiry = time.time() + 3000
        return self._app_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._send(
            method, path, params=params, json_body=json_body, headers=headers
        )
        if response.status_code >= 400:
            raise self._error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._token()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AgentHub/0.1",
        }
        if headers:
            request_headers.update(headers)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                f"{GITHUB_API}{path}",
                params=params,
                json=json_body,
                headers=request_headers,
            )
        return response

    @staticmethod
    def _error(response: httpx.Response) -> GitHubError:
        try:
            message = response.json().get("message", "request failed")
        except Exception:
            message = "request failed"
        retry_after = response.headers.get("Retry-After")
        delay = int(retry_after) if retry_after and retry_after.isdigit() else None
        if retry_after and delay is None:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = max(1, int((retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError):
                delay = None
        if delay is None and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = response.headers.get("X-RateLimit-Reset")
            if reset and reset.isdigit():
                delay = max(1, int(reset) - int(time.time()))
        return GitHubError(response.status_code, str(message)[:300], delay)

    async def prepare_pull_request(
        self, artifact: dict[str, Any], agent_name: str
    ) -> dict[str, Any]:
        owner = artifact["repository_owner"]
        repo = artifact["repository_name"]
        base = self.settings.github_default_branch
        repo_info = await self.request("GET", f"/repos/{owner}/{repo}")
        base = repo_info.get("default_branch") or base
        head = await self.request("GET", f"/repos/{owner}/{repo}/branches/{quote(base, safe='')}")
        default_sha = head["commit"]["sha"]
        branch = artifact["branch_name"]
        branch_path = quote(branch, safe="/")
        try:
            await self.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch_path}")
        except GitHubError as exc:
            if exc.status_code != 404:
                raise
            await self.request(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                json_body={"ref": f"refs/heads/{branch}", "sha": default_sha},
            )

        file_path = quote(artifact["file_path"], safe="/")
        file_data = await self._optional(
            "GET", f"/repos/{owner}/{repo}/contents/{file_path}", params={"ref": branch}
        )
        body = self.render_artifact(artifact, agent_name)
        content_args = {
            "message": f"AgentHub: publish {artifact['title']}",
            "content": base64.b64encode(body.encode()).decode(),
            "branch": branch,
        }
        if isinstance(file_data, dict) and file_data.get("sha"):
            content_args["sha"] = file_data["sha"]
        file_result = await self.request(
            "PUT", f"/repos/{owner}/{repo}/contents/{file_path}", json_body=content_args
        )
        commit_sha = file_result["commit"]["sha"]
        pulls = await self.request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "base": base, "per_page": 100},
        )
        pull = next((p for p in pulls if p.get("head", {}).get("ref") == branch), None)
        if pull and pull.get("state") == "closed" and not pull.get("merged_at"):
            pull = await self.request(
                "PATCH",
                f"/repos/{owner}/{repo}/pulls/{pull['number']}",
                json_body={"state": "open"},
            )
        elif not pull:
            pull = await self.request(
                "POST",
                f"/repos/{owner}/{repo}/pulls",
                json_body={
                    "title": f"Knowledge: {artifact['title']}",
                    "head": branch,
                    "base": base,
                    "body": f"AgentHub artifact `{artifact['id']}`. Review and merge to publish.",
                },
            )
        return {
            "branch_commit_sha": commit_sha,
            "pull_request_number": pull["number"],
            "pull_request_url": pull["html_url"],
        }

    async def get_pull_request(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/repos/{artifact['repository_owner']}/{artifact['repository_name']}/pulls/{artifact['pull_request_number']}",
        )

    async def get_pull_request_conditional(
        self, artifact: dict[str, Any], etag: str | None = None
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        headers = {"If-None-Match": etag} if etag else None
        response = await self._send(
            "GET",
            f"/repos/{artifact['repository_owner']}/{artifact['repository_name']}/pulls/{artifact['pull_request_number']}",
            headers=headers,
        )
        response_etag = response.headers.get("ETag") or etag
        if response.status_code == 304:
            return None, response_etag, True
        if response.status_code >= 400:
            raise self._error(response)
        data = response.json() if response.content else None
        return data, response_etag, False

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        data = await self.request("GET", f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}")
        return data["commit"]["sha"]

    async def fetch_file(self, artifact: dict[str, Any], ref: str) -> str:
        path = quote(artifact["file_path"], safe="/")
        data = await self.request(
            "GET",
            f"/repos/{artifact['repository_owner']}/{artifact['repository_name']}/contents/{path}",
            params={"ref": ref},
        )
        if data.get("encoding") != "base64":
            raise GitHubError(422, "GitHub did not return a base64 file")
        return base64.b64decode(data["content"]).decode("utf-8")

    async def _optional(self, method: str, path: str, **kwargs):
        try:
            return await self.request(method, path, **kwargs)
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise

    @staticmethod
    def render_artifact(artifact: dict[str, Any], agent_name: str) -> str:
        import json

        front = {
            "agenthub_id": artifact["id"],
            "project": artifact["project_slug"],
            "type": artifact["artifact_type"],
            "version": artifact["version"],
            "created_by": agent_name,
            "task_id": artifact["task_id"],
            "created_at": artifact["created_at"],
        }
        # JSON-quoted scalars are valid YAML strings and avoid front-matter injection.
        return (
            "---\n"
            + "\n".join(
                f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()
            )
            + "\n---\n\n"
            + artifact["content"]
        )


def strip_front_matter(content: str) -> str:
    match = FRONT_MATTER_RE.match(content)
    if match is None:
        return content
    body = content[match.end() :]
    if body.startswith("\r\n"):
        return body[2:]
    if body.startswith("\n"):
        return body[1:]
    return body
