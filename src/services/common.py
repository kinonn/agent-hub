"""Shared service-layer helpers for authorization, serialization, and events."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.types import utc_now
from src.errors import fail, forbidden, not_found
from src.security.tokens import Principal


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_json(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(data: Any) -> str:
    try:
        encoded = canonical_json(data).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise fail("validation_error", "Request contains invalid JSON or text.", 422) from exc
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(secret: bytes, filters: dict[str, Any], values: list[Any]) -> str:
    payload = canonical_json({"f": filters, "v": values}).encode()
    signature = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def decode_cursor(secret: bytes, cursor: str, filters: dict[str, Any]) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw[:-16], raw[-16:]
        expected = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        decoded = json.loads(payload)
        if decoded.get("f") != filters or not isinstance(decoded.get("v"), list):
            raise ValueError("cursor filters mismatch")
        return decoded["v"]
    except Exception as exc:
        raise fail(
            "validation_error", "Cursor is invalid or does not match these filters.", 422
        ) from exc


async def one(
    conn: AsyncConnection, sql: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    row = (await conn.execute(text(sql), params or {})).mappings().first()
    return dict(row) if row is not None else None


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise forbidden("insufficient_role")


async def all_rows(
    conn: AsyncConnection, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    rows = (await conn.execute(text(sql), params or {})).mappings().all()
    return [dict(row) for row in rows]


async def resolve_project(conn: AsyncConnection, project_ref: str) -> dict[str, Any]:
    project = await one(
        conn,
        "SELECT * FROM projects WHERE (id=:ref OR slug=:ref) AND is_active=1",
        {"ref": project_ref},
    )
    if project is None:
        raise not_found()
    return project


async def require_project_access(
    conn: AsyncConnection,
    project_id: str,
    principal: Principal,
    *,
    roles: tuple[str, ...] | None = None,
) -> str:
    if principal.is_agent:
        row = await one(
            conn,
            """SELECT pa.role FROM project_agents pa JOIN projects p ON p.id=pa.project_id
               JOIN agents a ON a.id=pa.agent_id WHERE pa.project_id=:project AND pa.agent_id=:actor
                 AND p.is_active=1 AND a.status='active'""",
            {"project": project_id, "actor": principal.id},
        )
        if row is None:
            raise not_found()
        if roles and row["role"] not in roles:
            raise forbidden("insufficient_role")
        return row["role"]
    if principal.is_admin:
        return "maintainer"
    row = await one(
        conn,
        """SELECT upa.role FROM user_project_access upa JOIN projects p ON p.id=upa.project_id
           WHERE upa.project_id=:project AND upa.user_id=:actor AND p.is_active=1""",
        {"project": project_id, "actor": principal.id},
    )
    if row is None:
        raise not_found()
    if roles and row["role"] not in roles and row["role"] != "maintainer":
        raise forbidden("insufficient_role")
    return row["role"]


async def require_agent_project(
    conn: AsyncConnection,
    project_id: str,
    principal: Principal,
    roles: tuple[str, ...] = ("reader", "contributor", "maintainer"),
) -> str:
    if not principal.is_agent:
        raise forbidden("insufficient_role")
    row = await one(
        conn,
        """SELECT pa.role,a.status,p.is_active FROM project_agents pa
           JOIN agents a ON a.id=pa.agent_id JOIN projects p ON p.id=pa.project_id
           WHERE pa.project_id=:project AND pa.agent_id=:agent""",
        {"project": project_id, "agent": principal.id},
    )
    if row is None:
        raise not_found()
    if row["status"] != "active" or not row["is_active"]:
        raise forbidden("forbidden")
    if row["role"] not in roles:
        raise forbidden("insufficient_role")
    return row["role"]


async def require_human_project(
    conn: AsyncConnection,
    project_id: str,
    principal: Principal,
    *,
    maintain: bool = False,
) -> str:
    if not principal.is_user:
        raise forbidden("insufficient_role")
    if principal.is_admin:
        return "maintainer"
    row = await one(
        conn,
        """SELECT upa.role FROM user_project_access upa JOIN projects p ON p.id=upa.project_id
           WHERE upa.project_id=:project AND upa.user_id=:user AND p.is_active=1""",
        {"project": project_id, "user": principal.id},
    )
    if row is None:
        raise not_found()
    if maintain and row["role"] != "maintainer":
        raise forbidden("insufficient_role")
    return row["role"]


async def insert_audit(
    conn: AsyncConnection,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    now: str | None = None,
) -> None:
    await conn.execute(
        text("""INSERT INTO audit_logs(id,actor_type,actor_id,action,project_id,resource_type,resource_id,metadata,created_at)
                VALUES(:id,:actor_type,:actor_id,:action,:project,:resource_type,:resource_id,:metadata,:now)"""),
        {
            "id": new_id(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "project": project_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metadata": canonical_json(metadata) if metadata is not None else None,
            "now": now or utc_now(),
        },
    )


async def record_task_event(
    conn: AsyncConnection,
    task_id: str,
    actor_agent_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    now: str | None = None,
    actor: Principal | None = None,
) -> None:
    event_time = now or utc_now()
    await conn.execute(
        text(
            "INSERT INTO task_events(id,task_id,actor_agent_id,event_type,payload,created_at) VALUES(:id,:task,:actor,:type,:payload,:now)"
        ),
        {
            "id": new_id(),
            "task": task_id,
            "actor": actor_agent_id,
            "type": event_type,
            "payload": canonical_json(payload) if payload is not None else None,
            "now": event_time,
        },
    )
    if event_type not in {"heartbeat", "progress"}:
        task = await one(conn, "SELECT project_id FROM tasks WHERE id=:task", {"task": task_id})
        actor_type = actor.kind if actor else ("agent" if actor_agent_id else "system")
        actor_id = actor.id if actor else (actor_agent_id or "system")
        await insert_audit(
            conn,
            actor_type=actor_type,
            actor_id=actor_id,
            action=f"task.{event_type}",
            resource_type="task",
            resource_id=task_id,
            project_id=task["project_id"] if task else None,
            metadata={"event_type": event_type},
            now=event_time,
        )


async def emit_event(
    conn: AsyncConnection,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    project_id: str | None,
    payload: dict[str, Any] | None = None,
    *,
    now: str | None = None,
) -> int:
    status = "not_required"
    if project_id and event_type == "task.available":
        adapter = await one(
            conn,
            """SELECT 1 AS yes FROM project_agents pa JOIN agents a ON a.id=pa.agent_id
               JOIN projects p ON p.id=pa.project_id
               WHERE pa.project_id=:project AND p.is_active=1 AND pa.role IN ('contributor','maintainer')
                 AND a.status='active' AND a.execution_mode='adapter' LIMIT 1""",
            {"project": project_id},
        )
        if adapter:
            status = "pending"
    result = await conn.execute(
        text("""INSERT INTO events_outbox(id,project_id,event_type,aggregate_type,aggregate_id,payload,status,created_at,next_attempt_at)
                VALUES(:id,:project,:type,:aggregate_type,:aggregate_id,:payload,:status,:now,:now)"""),
        {
            "id": new_id(),
            "project": project_id,
            "type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "payload": canonical_json(payload or {}),
            "status": status,
            "now": now or utc_now(),
        },
    )
    return int(result.lastrowid or 0)


async def fetch_task(conn: AsyncConnection, task_id: str) -> dict[str, Any]:
    task = await one(
        conn,
        """SELECT t.*,p.slug AS project_slug FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=:id""",
        {"id": task_id},
    )
    if task is None:
        raise not_found()
    return task


def task_dict(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "project": row.get("project_slug", ""),
        "task_type": row["task_type"],
        "title": row["title"],
        "status": row["status"],
        "priority": row["priority"],
        "assigned_agent_id": row["assigned_agent_id"],
        "target_capability": row["target_capability"],
        "attempt_count": row["attempt_count"],
        "available_at": row["available_at"],
        "created_at": row["created_at"],
        "description": row["description"],
        "input_payload": json.loads(row["input_payload"]) if row.get("input_payload") else None,
        "created_by_agent_id": row["created_by_agent_id"],
        "max_attempts": row["max_attempts"],
        "lease_owner_agent_id": row["lease_owner_agent_id"],
        "lease_expires_at": row["lease_expires_at"],
        "cancel_requested": bool(row["cancel_requested_at"]),
        "blocked_reason": row["blocked_reason"],
        "result_summary": row["result_summary"],
        "error_detail": row["error_detail"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    return result


async def fetch_artifact(conn: AsyncConnection, artifact_id: str) -> dict[str, Any]:
    row = await one(
        conn,
        """SELECT a.*,p.slug AS project_slug FROM artifacts a JOIN projects p ON p.id=a.project_id WHERE a.id=:id""",
        {"id": artifact_id},
    )
    if row is None:
        raise not_found()
    return row


def artifact_dict(
    row: dict[str, Any], include_content: bool = False, include_error: bool = False
) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "project": row.get("project_slug", ""),
        "task_id": row["task_id"],
        "created_by_agent_id": row["created_by_agent_id"],
        "artifact_type": row["artifact_type"],
        "title": row["title"],
        "summary": row["summary"],
        "document_key": row["document_key"],
        "version": row["version"],
        "publication_status": row["publication_status"],
        "repository": f"{row['repository_owner']}/{row['repository_name']}",
        "file_path": row["file_path"],
        "commit_sha": row["commit_sha"],
        "pull_request_number": row["pull_request_number"],
        "pull_request_url": row["pull_request_url"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
    }
    if include_content:
        result["content"] = row["content"]
    if include_error:
        result["last_error"] = row.get("last_error")
    return result


def message_dict(row: dict[str, Any], project_slug: str | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": project_slug or row.get("project_slug", ""),
        "sender_agent_id": row["sender_agent_id"],
        "recipient_agent_id": row["recipient_agent_id"],
        "thread_id": row["thread_id"],
        "subject": row["subject"],
        "body": row["body"],
        "message_type": row["message_type"],
        "created_at": row["created_at"],
        "read_at": row.get("read_at"),
        "acknowledged_at": row.get("acknowledged_at"),
    }


async def task_role_for_actor(conn: AsyncConnection, project_id: str, principal: Principal) -> str:
    if principal.is_agent:
        return await require_agent_project(conn, project_id, principal)
    return await require_human_project(conn, project_id, principal)


def error_not_leased() -> Exception:
    return fail("task_not_leased", "The task does not currently have an active lease.", 409)
