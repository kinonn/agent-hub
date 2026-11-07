from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.generate_mcp_schemas import _json_schema
from src.errors import AgentHubError
from src.mcp.server import mcp
from src.schemas.models import (
    TOOL_MODELS,
    AgentProfile,
    ArtifactSummary,
    ClaimTaskBody,
    ClaimTaskInput,
    CompleteTaskBody,
    CompleteTaskInput,
    CreateTaskBody,
    CreateTaskInput,
    HeartbeatTaskBody,
    HeartbeatTaskInput,
    LinkedArtifactContext,
    Message,
    ProjectContext,
    PublishArtifactBody,
    PublishArtifactInput,
    PublishMessageBody,
    PublishMessageInput,
    UpdateTaskBody,
    UpdateTaskInput,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "docs" / "mcp-schemas"


def test_committed_tool_schemas_match_runtime_tools():
    runtime = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(runtime) == set(TOOL_MODELS)
    schema_files = {
        path.stem
        for path in SCHEMA_DIR.glob("*.json")
        if path.name not in {"common.json", "entities.json"}
    }
    assert schema_files == set(TOOL_MODELS)
    for name, (input_model, output_model) in TOOL_MODELS.items():
        document = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
        assert document["input"] == _json_schema(runtime[name].parameters)
        assert document["output"] == _json_schema(runtime[name].output_schema)
        assert document["input"] == _json_schema(input_model.model_json_schema(mode="validation"))
        assert document["output"] == _json_schema(
            output_model.model_json_schema(mode="serialization")
        )
        assert document["input"].get("additionalProperties") is False


def test_tool_models_reject_unknown_fields_and_missing_artifact_lease_token():
    with pytest.raises(ValidationError):
        TOOL_MODELS["register_agent"][0].model_validate({"unknown": True})
    with pytest.raises(ValidationError):
        TOOL_MODELS["publish_artifact"][0].model_validate(
            {
                "project": "sample-project",
                "artifact_type": "research",
                "title": "Title",
                "content": "Body",
                "task_id": "00000000-0000-0000-0000-000000000000",
            }
        )


def test_rest_request_bodies_reuse_mcp_field_contracts():
    shared_fields = (
        (PublishMessageInput, PublishMessageBody, "project"),
        (CreateTaskInput, CreateTaskBody, "project"),
        (ClaimTaskInput, ClaimTaskBody, "project"),
        (UpdateTaskInput, UpdateTaskBody, "task_id"),
        (HeartbeatTaskInput, HeartbeatTaskBody, "task_id"),
        (CompleteTaskInput, CompleteTaskBody, "task_id"),
        (PublishArtifactInput, PublishArtifactBody, "project"),
    )
    for mcp_model, rest_model, path_field in shared_fields:
        mcp_schema = _json_schema(mcp_model.model_json_schema(mode="validation"))
        rest_schema = _json_schema(rest_model.model_json_schema(mode="validation"))
        mcp_properties = dict(mcp_schema.pop("properties"))
        mcp_properties.pop(path_field)
        assert mcp_properties == rest_schema.pop("properties")
        mcp_required = set(mcp_schema.pop("required", []))
        mcp_required.discard(path_field)
        assert mcp_required == set(rest_schema.pop("required", []))
        for key in ("additionalProperties", "allOf", "dependentRequired"):
            assert mcp_schema.get(key) == rest_schema.get(key)


def test_optional_shared_entity_fields_are_optional_in_generated_contracts():
    optional = {
        AgentProfile: {"description", "last_seen_at"},
        Message: {"sender_agent_id", "recipient_agent_id", "thread_id"},
        ProjectContext: {"instructions"},
        LinkedArtifactContext: {"commit_sha"},
        ArtifactSummary: {"repository"},
    }
    for model, fields in optional.items():
        required = set(model.model_json_schema(mode="serialization").get("required", []))
        assert fields.isdisjoint(required)


def test_rate_limited_and_unavailable_errors_have_retry_after():
    assert AgentHubError("rate_limited", "Slow down", 429).headers["Retry-After"] == "60"
    assert AgentHubError("unavailable", "Try again", 503).headers["Retry-After"] == "1"
