from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from src.schemas.models import StrictModel

ProjectSlug = Annotated[
    str, StringConstraints(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
]
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_email(value: str) -> str:
    if not EMAIL_RE.fullmatch(value):
        raise ValueError("email address is invalid")
    return value


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320, json_schema_extra={"format": "email"})
    password: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_email(self):
        _validate_email(self.email)
        return self


class CreateUserRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320, json_schema_extra={"format": "email"})
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "member"] = "member"

    @model_validator(mode="after")
    def validate_email(self):
        _validate_email(self.email)
        return self


class PatchUserRequest(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None


class CreateAgentRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    agent_type: Literal["hermes", "opencode", "copilot", "other"] = "other"
    description: str | None = Field(default=None, max_length=1000)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    execution_mode: Literal["poll", "adapter"] = "poll"
    adapter_config: dict[str, Any] | None = None


class PatchAgentRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    agent_type: Literal["hermes", "opencode", "copilot", "other"] | None = None
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["active", "disabled"] | None = None
    execution_mode: Literal["poll", "adapter"] | None = None
    capabilities: list[str] | None = Field(default=None, max_length=32)
    adapter_config: dict[str, Any] | None = None


class CreateProjectRequest(StrictModel):
    slug: ProjectSlug
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=20000)


class PatchProjectRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=20000)
    is_active: bool | None = None


class AgentMembershipRequest(StrictModel):
    role: Literal["reader", "contributor", "maintainer"]


class UserMembershipRequest(StrictModel):
    role: Literal["viewer", "maintainer"]


class UserMembership(StrictModel):
    role: Literal["viewer", "maintainer"]
