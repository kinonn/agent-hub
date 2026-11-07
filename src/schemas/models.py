from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase

UUIDPattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
PROJECT_REF_PATTERN = r"^(?:[a-z0-9][a-z0-9-]{1,62}|" + UUIDPattern[1:-1] + r")$"
UUID = Annotated[str, StringConstraints(pattern=UUIDPattern)]
ProjectRef = Annotated[str, StringConstraints(pattern=PROJECT_REF_PATTERN)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Limit = Annotated[int, Field(ge=1, le=100)]
LeaseToken = Annotated[
    str, StringConstraints(min_length=22, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]
IdempotencyKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
Capability = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
TaskType = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


def _valid_document_key(value: str) -> str:
    if ".." in value:
        raise ValueError("document_key must not contain consecutive dots")
    return value


DocumentKey = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$"),
    AfterValidator(_valid_document_key),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,79}$",
            "minLength": 1,
            "maxLength": 80,
        }
    ),
]


def _valid_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is not a valid date-time") from exc
    if (
        parsed.tzinfo != UTC
        or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value
    ):
        raise ValueError("timestamp must use canonical UTC millisecond format")
    return value


Timestamp = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"),
    AfterValidator(_valid_timestamp),
]

AgentType = Literal["hermes", "opencode", "copilot", "other"]
AgentStatus = Literal["active", "disabled"]
TaskStatus = Literal[
    "pending", "claimed", "in_progress", "blocked", "completed", "failed", "cancelled"
]
ArtifactType = Literal["research", "specification", "decision", "implementation", "review"]
PublicationStatus = Literal["draft", "in_review", "published", "rejected", "superseded", "failed"]
MessageType = Literal["info", "question", "answer", "status", "handoff", "system"]
ProjectRole = Literal["reader", "contributor", "maintainer"]


class StrictModel(ArgModelBase):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorBody


class AgentProfile(StrictModel):
    id: UUID
    name: str
    agent_type: AgentType
    description: str | None = None
    status: AgentStatus
    capabilities: list[Capability]
    last_seen_at: Timestamp | None = None


class AgentMember(AgentProfile):
    role: ProjectRole


class Membership(StrictModel):
    project: str
    role: ProjectRole


class ProjectSummary(StrictModel):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    instructions: str | None = None
    is_active: bool = True


class Message(StrictModel):
    id: UUID
    project: str
    sender_agent_id: UUID | None = None
    recipient_agent_id: UUID | None = None
    thread_id: UUID | None = None
    subject: str
    body: str
    message_type: MessageType
    created_at: Timestamp
    read_at: Timestamp | None = None
    acknowledged_at: Timestamp | None = None


class Receipt(StrictModel):
    message_id: UUID
    agent_id: UUID
    read_at: Timestamp | None = None
    acknowledged_at: Timestamp | None = None


class TaskSummary(StrictModel):
    id: UUID
    project: str
    task_type: TaskType
    title: str
    status: TaskStatus
    priority: int = Field(ge=-100, le=100)
    assigned_agent_id: UUID | None = None
    target_capability: str | None = None
    attempt_count: int = Field(ge=0)
    available_at: Timestamp
    created_at: Timestamp
    updated_at: Timestamp | None = None


class Task(TaskSummary):
    description: str | None = None
    input_payload: Any = None
    created_by_agent_id: UUID
    max_attempts: int = Field(ge=1, le=10)
    lease_owner_agent_id: UUID | None = None
    lease_expires_at: Timestamp | None = None
    cancel_requested: bool
    blocked_reason: str | None = None
    result_summary: str | None = None
    error_detail: str | None = None
    updated_at: Timestamp
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None


class ArtifactSummary(StrictModel):
    id: UUID
    project: str
    task_id: UUID | None = None
    created_by_agent_id: UUID | None = None
    artifact_type: ArtifactType
    title: str
    summary: str | None = None
    document_key: DocumentKey
    version: int = Field(ge=1)
    publication_status: PublicationStatus
    repository: str = Field(default_factory=str)
    file_path: str
    commit_sha: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    created_at: Timestamp
    published_at: Timestamp | None = None


class Artifact(ArtifactSummary):
    content: str | None = None
    last_error: str | None = None


class SearchResult(StrictModel):
    artifact_id: UUID
    project: str
    artifact_type: ArtifactType
    title: str
    snippet: str
    version: int
    publication_status: PublicationStatus
    repository: str
    file_path: str
    commit_sha: str | None = None
    pull_request_url: str | None = None
    score: float = Field(default_factory=lambda: 0.0)


class ActiveLease(StrictModel):
    task_id: UUID
    project: str
    lease_expires_at: Timestamp


class LeaseInfo(StrictModel):
    lease_token: LeaseToken
    lease_expires_at: Timestamp
    heartbeat_interval_seconds: int = Field(ge=1)


class ProjectContext(StrictModel):
    slug: str
    name: str
    instructions: str | None = None


class LinkedArtifactContext(StrictModel):
    id: UUID
    title: str
    artifact_type: ArtifactType
    document_key: DocumentKey
    version: int = Field(ge=1)
    publication_status: PublicationStatus
    file_path: str
    commit_sha: str | None = None


class ClaimContext(StrictModel):
    project: ProjectContext
    artifacts: list[LinkedArtifactContext] = Field(default_factory=list)


class ClaimedTask(StrictModel):
    id: UUID
    project: str
    task_type: TaskType
    title: str
    description: str | None = None
    input_payload: Any = None
    status: Literal["claimed", "in_progress"]
    priority: int
    assigned_agent_id: UUID | None = None
    target_capability: str | None = None
    attempt_count: int
    max_attempts: int
    available_at: Timestamp
    created_at: Timestamp
    updated_at: Timestamp
    created_by_agent_id: UUID
    lease_owner_agent_id: UUID
    lease_expires_at: Timestamp
    cancel_requested: bool
    blocked_reason: str | None = None
    result_summary: str | None = None
    error_detail: str | None = None
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None
    lease: LeaseInfo
    context: ClaimContext


class RegisterAgentInput(StrictModel):
    description: str | None = Field(default=None, max_length=1000)
    capabilities: list[Capability] = Field(
        default_factory=list, max_length=32, json_schema_extra={"uniqueItems": True}
    )

    @model_validator(mode="after")
    def capabilities_unique(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self


class GetAgentStatusInput(StrictModel):
    agent_id: UUID | None = None


class ListAgentsInput(StrictModel):
    project: ProjectRef


class PublishMessageFields(StrictModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=65536)
    message_type: MessageType
    recipient_agent_id: UUID | None = None
    thread_id: UUID | None = None
    idempotency_key: IdempotencyKey | None = None


class PublishMessageInput(PublishMessageFields):
    project: ProjectRef


class MessageReadInput(StrictModel):
    acknowledge: bool = False


class GetMessagesInput(StrictModel):
    project: ProjectRef
    unread_only: bool = False
    thread_id: UUID | None = None
    since: Timestamp | None = None
    limit: Limit = 25
    cursor: Cursor | None = None


class MarkMessageReadInput(StrictModel):
    message_id: UUID
    acknowledge: bool = False


class CreateTaskFields(StrictModel):
    task_type: TaskType
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    input_payload: dict[str, Any] | None = None
    assigned_agent_id: UUID | None = None
    target_capability: Capability | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    available_at: Timestamp | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    idempotency_key: IdempotencyKey | None = None


class CreateTaskInput(CreateTaskFields):
    project: ProjectRef


class GetTaskInput(StrictModel):
    task_id: UUID


class GetPendingTasksInput(StrictModel):
    project: ProjectRef
    claimable_only: bool = True
    limit: Limit = 25
    cursor: Cursor | None = None


class ClaimTaskFields(StrictModel):
    task_id: UUID | None = None
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)


class ClaimTaskInput(ClaimTaskFields):
    project: ProjectRef


class UpdateTaskFields(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "blocked"}}, "required": ["status"]},
                    "then": {"required": ["blocked_reason"]},
                },
                {
                    "if": {"properties": {"status": {"const": "failed"}}, "required": ["status"]},
                    "then": {"required": ["error_detail"]},
                },
            ]
        },
    )
    lease_token: LeaseToken | None = None
    status: Literal["in_progress", "blocked", "pending", "failed", "cancelled"] | None = None
    progress_note: str | None = Field(default=None, max_length=2000)
    blocked_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    error_detail: str | None = Field(default=None, min_length=1, max_length=4000)
    retryable: bool = True

    @model_validator(mode="after")
    def check_status_fields(self):
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked_reason is required when status is blocked")
        if self.status == "failed" and not self.error_detail:
            raise ValueError("error_detail is required when status is failed")
        return self


class UpdateTaskInput(UpdateTaskFields):
    task_id: UUID


class HeartbeatTaskFields(StrictModel):
    lease_token: LeaseToken
    progress_note: str | None = Field(default=None, max_length=2000)


class HeartbeatTaskInput(HeartbeatTaskFields):
    task_id: UUID


class CompleteTaskFields(StrictModel):
    lease_token: LeaseToken
    result_summary: str = Field(min_length=1, max_length=20000)
    artifact_ids: list[UUID] = Field(
        default_factory=list, max_length=50, json_schema_extra={"uniqueItems": True}
    )

    @model_validator(mode="after")
    def artifact_ids_unique(self):
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids must be unique")
        return self


class CompleteTaskInput(CompleteTaskFields):
    task_id: UUID


class PublishArtifactFields(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"dependentRequired": {"task_id": ["lease_token"]}},
    )
    artifact_type: ArtifactType
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=262144)
    summary: str | None = Field(default=None, max_length=1000)
    document_key: DocumentKey | None = None
    task_id: UUID | None = None
    lease_token: LeaseToken | None = None
    idempotency_key: IdempotencyKey | None = None

    @model_validator(mode="after")
    def task_requires_lease_token(self):
        if self.task_id and not self.lease_token:
            raise ValueError("lease_token is required when task_id is supplied")
        return self


class PublishArtifactInput(PublishArtifactFields):
    project: ProjectRef


class PublishArtifactBody(PublishArtifactFields):
    """REST request body; the project reference is carried in the URL path."""


class PublishMessageBody(PublishMessageFields):
    """REST request body; the project reference is carried in the URL path."""


class CreateTaskBody(CreateTaskFields):
    """REST request body; the project reference is carried in the URL path."""


class ClaimTaskBody(ClaimTaskFields):
    """REST request body; the project reference is carried in the URL path."""


class UpdateTaskBody(UpdateTaskFields):
    """REST request body; the task ID is carried in the URL path."""


class HeartbeatTaskBody(HeartbeatTaskFields):
    """REST request body; the task ID is carried in the URL path."""


class CompleteTaskBody(CompleteTaskFields):
    """REST request body; the task ID is carried in the URL path."""


class GetArtifactInput(StrictModel):
    artifact_id: UUID
    include_content: bool = False


class SearchKnowledgeInput(StrictModel):
    project: ProjectRef
    query: str = Field(min_length=1, max_length=500)
    artifact_type: ArtifactType | None = None
    include_unpublished: bool = False
    limit: int = Field(default=10, ge=1, le=20)


class ListProjectArtifactsInput(StrictModel):
    project: ProjectRef
    status: PublicationStatus | None = None
    artifact_type: ArtifactType | None = None
    limit: Limit = 25
    cursor: Cursor | None = None


class RegisterAgentOutput(StrictModel):
    agent: AgentProfile
    memberships: list[Membership]


class GetAgentStatusOutput(StrictModel):
    agent: AgentProfile
    active_leases: list[ActiveLease]


class ListAgentsOutput(StrictModel):
    agents: list[AgentMember]


class PublishMessageOutput(StrictModel):
    message: Message
    idempotent_replay: bool


class GetMessagesOutput(StrictModel):
    messages: list[Message]
    next_cursor: str | None


class MarkMessageReadOutput(StrictModel):
    receipt: Receipt


class CreateTaskOutput(StrictModel):
    task: Task
    idempotent_replay: bool


class GetTaskOutput(StrictModel):
    task: Task


class GetPendingTasksOutput(StrictModel):
    tasks: list[TaskSummary]
    next_cursor: str | None


class ClaimTaskOutput(StrictModel):
    task: ClaimedTask | None


class UpdateTaskOutput(StrictModel):
    task: Task
    cancel_requested: bool
    idempotent_replay: bool


class HeartbeatTaskOutput(StrictModel):
    lease_expires_at: Timestamp
    cancel_requested: bool
    max_runtime_deadline: Timestamp


class CompleteTaskOutput(StrictModel):
    task: Task
    idempotent_replay: bool


class PublishArtifactOutput(StrictModel):
    artifact: Artifact
    idempotent_replay: bool


class GetArtifactOutput(StrictModel):
    artifact: Artifact


class SearchKnowledgeOutput(StrictModel):
    results: list[SearchResult]


class ListProjectArtifactsOutput(StrictModel):
    artifacts: list[ArtifactSummary]
    next_cursor: str | None


TOOL_MODELS: dict[str, tuple[type[StrictModel], type[StrictModel]]] = {
    "register_agent": (RegisterAgentInput, RegisterAgentOutput),
    "get_agent_status": (GetAgentStatusInput, GetAgentStatusOutput),
    "list_agents": (ListAgentsInput, ListAgentsOutput),
    "publish_message": (PublishMessageInput, PublishMessageOutput),
    "get_messages": (GetMessagesInput, GetMessagesOutput),
    "mark_message_read": (MarkMessageReadInput, MarkMessageReadOutput),
    "create_task": (CreateTaskInput, CreateTaskOutput),
    "get_task": (GetTaskInput, GetTaskOutput),
    "get_pending_tasks": (GetPendingTasksInput, GetPendingTasksOutput),
    "claim_task": (ClaimTaskInput, ClaimTaskOutput),
    "update_task": (UpdateTaskInput, UpdateTaskOutput),
    "heartbeat_task": (HeartbeatTaskInput, HeartbeatTaskOutput),
    "complete_task": (CompleteTaskInput, CompleteTaskOutput),
    "publish_artifact": (PublishArtifactInput, PublishArtifactOutput),
    "get_artifact": (GetArtifactInput, GetArtifactOutput),
    "search_knowledge": (SearchKnowledgeInput, SearchKnowledgeOutput),
    "list_project_artifacts": (ListProjectArtifactsInput, ListProjectArtifactsOutput),
}
