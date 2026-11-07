"""Shared request-validation translations used by REST and MCP transports."""

from __future__ import annotations

from pydantic import ValidationError

from src.config import Settings
from src.errors import AgentHubError


def payload_too_large_from_validation(
    error: ValidationError, settings: Settings
) -> AgentHubError | None:
    """Map schema string-length failures to byte-limit 413s when applicable."""
    byte_limits = {
        "body": settings.max_message_bytes,
        "content": settings.max_artifact_bytes,
    }
    for item in error.errors():
        if item.get("type") != "string_too_long":
            continue
        field = next(
            (part for part in reversed(item.get("loc", ())) if isinstance(part, str)), None
        )
        maximum = byte_limits.get(field)
        value = item.get("input")
        if maximum is None or not isinstance(value, str):
            continue
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            continue
        if size > maximum:
            label = "Message body" if field == "body" else "Artifact content"
            return AgentHubError(
                "payload_too_large",
                f"{label} exceeds its configured byte limit.",
                413,
            )
    return None
