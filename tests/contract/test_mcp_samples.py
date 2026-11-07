from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.schemas.models import TOOL_MODELS

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "docs" / "mcp-schemas"
UUID = "00000000-0000-0000-0000-000000000001"
UUID_2 = "00000000-0000-0000-0000-000000000002"
STAMP = "2026-01-02T03:04:05.006Z"
TOKEN = "a" * 32


def _samples() -> tuple[dict[str, dict], dict[str, dict]]:
    agent = {
        "id": UUID,
        "name": "sample-agent",
        "agent_type": "other",
        "description": None,
        "status": "active",
        "capabilities": ["research"],
        "last_seen_at": None,
    }
    message = {
        "id": UUID,
        "project": "sample-project",
        "sender_agent_id": UUID,
        "recipient_agent_id": None,
        "thread_id": None,
        "subject": "Sample subject",
        "body": "Sample message body",
        "message_type": "info",
        "created_at": STAMP,
        "read_at": None,
        "acknowledged_at": None,
    }
    task_summary = {
        "id": UUID,
        "project": "sample-project",
        "task_type": "research",
        "title": "Sample task",
        "status": "pending",
        "priority": 0,
        "assigned_agent_id": None,
        "target_capability": "research",
        "attempt_count": 0,
        "available_at": STAMP,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    task = {
        **task_summary,
        "description": None,
        "input_payload": {"question": "What is WAL?"},
        "created_by_agent_id": UUID,
        "max_attempts": 3,
        "lease_owner_agent_id": None,
        "lease_expires_at": None,
        "cancel_requested": False,
        "blocked_reason": None,
        "result_summary": None,
        "error_detail": None,
        "started_at": None,
        "completed_at": None,
    }
    claimed_task = {
        **task,
        "status": "claimed",
        "lease_owner_agent_id": UUID,
        "lease_expires_at": STAMP,
        "lease": {
            "lease_token": TOKEN,
            "lease_expires_at": STAMP,
            "heartbeat_interval_seconds": 40,
        },
        "context": {
            "project": {"slug": "sample-project", "name": "Sample Project"},
            "artifacts": [],
        },
    }
    artifact = {
        "id": UUID,
        "project": "sample-project",
        "task_id": None,
        "created_by_agent_id": UUID,
        "artifact_type": "research",
        "title": "Sample artifact",
        "summary": None,
        "document_key": "sample-artifact",
        "version": 1,
        "publication_status": "draft",
        "repository": "example-owner/agenthub-knowledge",
        "file_path": "projects/sample-project/research/sample-artifact.md",
        "commit_sha": None,
        "pull_request_number": None,
        "pull_request_url": None,
        "created_at": STAMP,
        "published_at": None,
        "content": "Sample artifact body",
        "last_error": None,
    }
    inputs = {
        "register_agent": {},
        "get_agent_status": {},
        "list_agents": {"project": "sample-project"},
        "publish_message": {
            "project": "sample-project",
            "subject": "Sample subject",
            "body": "Sample message body",
            "message_type": "info",
        },
        "get_messages": {"project": "sample-project"},
        "mark_message_read": {"message_id": UUID},
        "create_task": {
            "project": "sample-project",
            "task_type": "research",
            "title": "Sample task",
        },
        "get_task": {"task_id": UUID},
        "get_pending_tasks": {"project": "sample-project"},
        "claim_task": {"project": "sample-project"},
        "update_task": {
            "task_id": UUID,
            "status": "blocked",
            "blocked_reason": "Needs more context",
        },
        "heartbeat_task": {"task_id": UUID, "lease_token": TOKEN},
        "complete_task": {
            "task_id": UUID,
            "lease_token": TOKEN,
            "result_summary": "Completed the task",
        },
        "publish_artifact": {
            "project": "sample-project",
            "artifact_type": "research",
            "title": "Sample artifact",
            "content": "Sample artifact body",
        },
        "get_artifact": {"artifact_id": UUID},
        "search_knowledge": {"project": "sample-project", "query": "sqlite wal"},
        "list_project_artifacts": {"project": "sample-project"},
    }
    outputs = {
        "register_agent": {
            "agent": agent,
            "memberships": [{"project": "sample-project", "role": "contributor"}],
        },
        "get_agent_status": {
            "agent": agent,
            "active_leases": [
                {"task_id": UUID, "project": "sample-project", "lease_expires_at": STAMP}
            ],
        },
        "list_agents": {"agents": [{**agent, "role": "contributor"}]},
        "publish_message": {"message": message, "idempotent_replay": False},
        "get_messages": {"messages": [message], "next_cursor": None},
        "mark_message_read": {
            "receipt": {"message_id": UUID, "agent_id": UUID_2, "read_at": STAMP}
        },
        "create_task": {"task": task, "idempotent_replay": False},
        "get_task": {"task": task},
        "get_pending_tasks": {"tasks": [task_summary], "next_cursor": None},
        "claim_task": {"task": claimed_task},
        "update_task": {"task": task, "cancel_requested": False, "idempotent_replay": False},
        "heartbeat_task": {
            "lease_expires_at": STAMP,
            "cancel_requested": False,
            "max_runtime_deadline": STAMP,
        },
        "complete_task": {
            "task": {**task, "status": "completed", "result_summary": "Completed"},
            "idempotent_replay": False,
        },
        "publish_artifact": {"artifact": artifact, "idempotent_replay": False},
        "get_artifact": {"artifact": artifact},
        "search_knowledge": {
            "results": [
                {
                    "artifact_id": UUID,
                    "project": "sample-project",
                    "artifact_type": "research",
                    "title": "Sample artifact",
                    "snippet": "Sample artifact body",
                    "version": 1,
                    "publication_status": "published",
                    "repository": "example-owner/agenthub-knowledge",
                    "file_path": "projects/sample-project/research/sample-artifact.md",
                }
            ]
        },
        "list_project_artifacts": {
            "artifacts": [
                {
                    key: value
                    for key, value in artifact.items()
                    if key not in {"content", "last_error"}
                }
            ],
            "next_cursor": None,
        },
    }
    return inputs, outputs


def test_every_tool_has_a_valid_sample_request_and_response():
    inputs, outputs = _samples()
    assert set(inputs) == set(TOOL_MODELS)
    assert set(outputs) == set(TOOL_MODELS)

    for name, (input_model, output_model) in TOOL_MODELS.items():
        document = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
        request = input_model.model_validate(inputs[name]).model_dump(mode="json")
        response = output_model.model_validate(outputs[name]).model_dump(mode="json")
        Draft202012Validator.check_schema(document["input"])
        Draft202012Validator.check_schema(document["output"])
        Draft202012Validator(document["input"]).validate(request)
        Draft202012Validator(document["output"]).validate(response)
