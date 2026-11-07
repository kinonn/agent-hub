"""Stable AgentHub API and MCP error model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentHubError(Exception):
    code: str
    message: str
    status: int = 400
    details: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == 401:
            self.headers.setdefault("WWW-Authenticate", "Bearer")
        elif self.status == 429:
            self.headers.setdefault("Retry-After", "60")
        elif self.status == 503:
            self.headers.setdefault("Retry-After", "1")

    def __str__(self) -> str:
        return self.message

    def envelope(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return {"error": result}


def fail(code: str, message: str, status: int, **details: Any) -> AgentHubError:
    return AgentHubError(code, message, status, details)


def not_found() -> AgentHubError:
    return fail("not_found", "The requested resource was not found.", 404)


def forbidden(code: str = "forbidden") -> AgentHubError:
    return fail(code, "You do not have permission to perform this operation.", 403)


def invalid_transition(message: str = "This task transition is not allowed.") -> AgentHubError:
    return fail("invalid_transition", message, 409)
