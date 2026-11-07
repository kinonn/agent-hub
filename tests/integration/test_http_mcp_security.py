from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.generate_mcp_schemas import _json_schema
from src.main import create_app
from src.mcp import server as mcp_module
from src.security.tokens import Principal
from src.services import (
    agent_service,
    artifact_service,
    auth_service,
    event_service,
    message_service,
    task_service,
)


@pytest.fixture
def test_app(harness, monkeypatch):
    database = harness["db"]
    webhook_secret = harness["tmp_path"] / "webhook-secret"
    webhook_secret.write_text("test-webhook-secret")
    settings = replace(
        harness["settings"],
        github_webhooks_enabled=True,
        github_webhook_secret_file=str(webhook_secret),
    )
    for service in (
        auth_service.auth_service,
        agent_service.agent_service,
        message_service.message_service,
        task_service.task_service,
        artifact_service.artifact_service,
    ):
        monkeypatch.setattr(service, "db", database)
        if hasattr(service, "settings"):
            monkeypatch.setattr(service, "settings", settings)
    monkeypatch.setattr(event_service.event_service, "db", database)
    monkeypatch.setattr(mcp_module, "db", database)
    app = create_app(database, settings)
    return TestClient(app)


def test_browser_and_mcp_auth_csrf_origin_and_validation(test_app, harness):
    import asyncio

    asyncio.run(
        harness["auth"].create_user("admin@example.com", "a-long-test-password", "Admin", "admin")
    )
    agent = asyncio.run(harness["auth"].create_agent("mcp-security-agent", "other"))
    with test_app as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "a-long-test-password"},
        )
        assert response.status_code == 200
        csrf = response.json()["csrf_token"]
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie

        denied = client.post("/api/v1/projects", json={"slug": "csrf-test", "name": "CSRF Test"})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "csrf_failed"
        created = client.post(
            "/api/v1/projects",
            json={"slug": "csrf-test", "name": "CSRF Test"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        assert created.json()["slug"] == "csrf-test"
        invalid_origin = client.post(
            "/mcp",
            headers={"Origin": "https://attacker.example", "Host": "testserver"},
            json={},
        )
        assert invalid_origin.status_code == 403

        unauthorized = client.post("/mcp", headers={"Host": "testserver"}, json={})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"

        headers = {
            "Authorization": f"Bearer {agent['token']}",
            "Host": "testserver",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
        )
        advertised = listed.json()["result"]["tools"]
        assert "get_task" in {tool["name"] for tool in advertised}
        for tool in advertised:
            committed = json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "docs"
                    / "mcp-schemas"
                    / f"{tool['name']}.json"
                ).read_text()
            )
            assert _json_schema(tool["inputSchema"]) == committed["input"]
            assert _json_schema(tool["outputSchema"]) == committed["output"]

        unknown_tool = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "missing_tool", "arguments": {}},
            },
        ).json()
        assert unknown_tool["error"]["code"] == -32601

        invalid_tool = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "register_agent", "arguments": {"unexpected": True}},
            },
        )
        error = invalid_tool.json()["result"]
        assert error["isError"] is True
        assert error["structuredContent"]["error"]["code"] == "validation_error"
        assert json.loads(error["content"][0]["text"]) == error["structuredContent"]

        success = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "register_agent", "arguments": {"capabilities": ["research"]}},
            },
        )
        result = success.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["agent"]["capabilities"] == ["research"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

        project = created.json()
        principal = Principal(kind="agent", id=agent["agent"]["id"], name="mcp-security-agent")
        asyncio.run(
            harness["auth"].set_agent_membership(project["id"], principal.id, "contributor")
        )
        oversized_artifact = client.post(
            f"/api/v1/projects/{project['slug']}/artifacts",
            headers={"Authorization": f"Bearer {agent['token']}"},
            json={
                "artifact_type": "research",
                "title": "Oversized body",
                "content": "x" * 262145,
            },
        )
        assert oversized_artifact.status_code == 413
        assert oversized_artifact.json()["error"]["code"] == "payload_too_large"

        oversized_mcp = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "publish_artifact",
                    "arguments": {
                        "project": project["slug"],
                        "artifact_type": "research",
                        "title": "Oversized body",
                        "content": "x" * 262145,
                    },
                },
            },
        ).json()["result"]
        assert oversized_mcp["isError"] is True
        assert oversized_mcp["structuredContent"]["error"]["code"] == "payload_too_large"

        task = asyncio.run(
            harness["tasks"].create_task(
                principal, project["slug"], "research", "MCP claim context"
            )
        )["task"]
        claimed = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "claim_task",
                    "arguments": {"project": project["slug"], "task_id": task["id"]},
                },
            },
        ).json()["result"]
        assert claimed["isError"] is False
        claimed_task = claimed["structuredContent"]["task"]
        assert claimed_task["context"]["project"]["slug"] == project["slug"]
        assert claimed_task["lease"]["lease_token"]

        webhook_payload = json.dumps(
            {
                "action": "closed",
                "repository": {"full_name": "example-owner/agenthub-knowledge"},
                "pull_request": {"number": 12},
            }
        ).encode()
        signature = (
            "sha256="
            + hmac.new(b"test-webhook-secret", webhook_payload, hashlib.sha256).hexdigest()
        )
        webhook_headers = {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "delivery-12",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        }
        invalid = client.post(
            "/api/v1/webhooks/github",
            headers={**webhook_headers, "X-Hub-Signature-256": "sha256=bad"},
            content=webhook_payload,
        )
        assert invalid.status_code == 401
        accepted = client.post(
            "/api/v1/webhooks/github", headers=webhook_headers, content=webhook_payload
        )
        duplicate = client.post(
            "/api/v1/webhooks/github", headers=webhook_headers, content=webhook_payload
        )
        assert accepted.status_code == 202 and accepted.json()["status"] == "accepted"
        assert duplicate.status_code == 202 and duplicate.json()["status"] == "duplicate"
