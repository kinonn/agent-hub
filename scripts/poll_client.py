#!/usr/bin/env python3
"""Reference MCP polling client. Agent task content is passed only as data."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _payload(result) -> dict[str, Any]:
    if result.isError:
        text = result.content[0].text if result.content else "MCP operation failed"
        try:
            error = json.loads(text).get("error", {})
            raise RuntimeError(
                f"AgentHub {error.get('code', 'error')}: {error.get('message', 'request failed')}"
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError("AgentHub MCP operation failed") from exc
    if result.structuredContent is not None:
        return result.structuredContent
    if result.content and getattr(result.content[0], "text", None):
        return json.loads(result.content[0].text)
    return {}


def _load_handler(spec: str) -> Callable:
    module_name, separator, function_name = spec.partition(":")
    if not separator:
        raise ValueError("AGENTHUB_HANDLER must use module:function syntax")
    function = getattr(importlib.import_module(module_name), function_name)
    if not callable(function):
        raise TypeError("Configured handler is not callable")
    return function


async def run() -> None:
    endpoint = os.environ.get("AGENTHUB_MCP_URL", "http://127.0.0.1:3000/mcp")
    token = os.environ.get("AGENTHUB_TOKEN")
    project = os.environ.get("AGENTHUB_PROJECT")
    handler_spec = os.environ.get("AGENTHUB_HANDLER")
    if not token or not project:
        raise SystemExit("Set AGENTHUB_TOKEN and AGENTHUB_PROJECT in the process environment.")
    if not handler_spec:
        raise SystemExit("Set AGENTHUB_HANDLER=trusted_module:function before claiming tasks.")
    handler = _load_handler(handler_spec)
    once = os.environ.get("AGENTHUB_ONCE", "false").lower() in {"1", "true", "yes"}
    poll_seconds = max(10, int(os.environ.get("AGENTHUB_POLL_SECONDS", "10")))
    heartbeat_seconds = max(5, int(os.environ.get("TASK_HEARTBEAT_SECONDS", "40")))

    async with streamablehttp_client(endpoint, headers={"Authorization": f"Bearer {token}"}) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            while True:
                claimed = _payload(
                    await session.call_tool(
                        "claim_task",
                        {"project": project, "instance_id": str(uuid.uuid4())},
                    )
                ).get("task")
                if claimed is None:
                    if once:
                        return
                    await asyncio.sleep(poll_seconds)
                    continue
                task = claimed
                task_id = task["id"]
                lease_token = task["lease"]["lease_token"]
                cancelled = asyncio.Event()
                lease_lost = asyncio.Event()
                print(
                    f"Claimed task {task_id} (attempt {task['attempt_count']}/{task['max_attempts']})."
                )
                await session.call_tool(
                    "update_task",
                    {
                        "task_id": task_id,
                        "lease_token": lease_token,
                        "status": "in_progress",
                        "progress_note": "Reference polling client started the configured handler.",
                    },
                )

                async def beat(
                    task_id=task_id,
                    lease_token=lease_token,
                    cancelled=cancelled,
                    lease_lost=lease_lost,
                ) -> None:
                    while True:
                        await asyncio.sleep(heartbeat_seconds)
                        try:
                            result = _payload(
                                await session.call_tool(
                                    "heartbeat_task",
                                    {"task_id": task_id, "lease_token": lease_token},
                                )
                            )
                        except Exception:
                            lease_lost.set()
                            return
                        if result.get("cancel_requested"):
                            cancelled.set()
                            return

                heartbeat = asyncio.create_task(beat())
                try:
                    # Handler is trusted, deployment-provided code. Retrieved task data is never
                    # interpolated into shell commands or treated as executable instructions.
                    outcome = await handler(task, session, cancelled)
                    if lease_lost.is_set():
                        return
                    if not isinstance(outcome, dict) or not isinstance(
                        outcome.get("result_summary"), str
                    ):
                        raise TypeError(
                            "handler must return result_summary and optional artifact_ids"
                        )
                    if cancelled.is_set():
                        await session.call_tool(
                            "update_task",
                            {
                                "task_id": task_id,
                                "lease_token": lease_token,
                                "status": "cancelled",
                            },
                        )
                    else:
                        await session.call_tool(
                            "complete_task",
                            {
                                "task_id": task_id,
                                "lease_token": lease_token,
                                "result_summary": outcome["result_summary"],
                                "artifact_ids": outcome.get("artifact_ids", []),
                            },
                        )
                except Exception as exc:
                    # Do not log task data or handler exception text; it may contain secrets.
                    try:
                        await session.call_tool(
                            "update_task",
                            {
                                "task_id": task_id,
                                "lease_token": lease_token,
                                "status": "failed",
                                "error_detail": f"handler failed ({type(exc).__name__})",
                                "retryable": True,
                            },
                        )
                    except Exception:
                        pass
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                    # Drop the only in-process reference to this lease token before polling again.
                    task.get("lease", {}).pop("lease_token", None)
                    lease_token = ""
                if once:
                    return


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
