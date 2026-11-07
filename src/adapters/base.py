from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentRecord:
    id: str
    name: str
    adapter_config: dict | None


@dataclass(frozen=True, slots=True)
class TaskSummary:
    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    healthy: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    delivered: bool
    detail: str | None = None


class AgentAdapter(Protocol):
    async def health(self, agent: AgentRecord) -> AdapterHealth: ...
    async def dispatch(self, agent: AgentRecord, task: TaskSummary) -> DispatchResult: ...
