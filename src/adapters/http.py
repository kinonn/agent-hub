from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from src.adapters.base import AdapterHealth, AgentRecord, DispatchResult, TaskSummary


class HTTPWakeupAdapter:
    """Dispatches a small, idempotent wake-up hint to a configured LAN endpoint."""

    def __init__(self, timeout_seconds: float = 30):
        self.timeout = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def health(self, agent: AgentRecord) -> AdapterHealth:
        return AdapterHealth(
            bool(agent.adapter_config and agent.adapter_config.get("url")),
            None if agent.adapter_config else "adapter URL is not configured",
        )

    async def dispatch(self, agent: AgentRecord, task: TaskSummary) -> DispatchResult:
        config = agent.adapter_config or {}
        url = config.get("url")
        if not isinstance(url, str) or not await self._allowed_lan_url(url):
            return DispatchResult(False, "adapter URL must be a valid HTTP(S) LAN endpoint")
        lock = self._locks.setdefault(agent.id, asyncio.Lock())
        async with lock:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json={
                        "task_id": task.id,
                        "title": task.title,
                        "event_id": config.get("event_id"),
                    },
                )
            if response.status_code >= 400:
                return DispatchResult(False, f"adapter returned HTTP {response.status_code}")
        return DispatchResult(True)

    @staticmethod
    async def _allowed_lan_url(url: str) -> bool:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return False
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except OSError:
            return False
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if not (address.is_private or address.is_loopback or address.is_link_local):
                return False
        return True
