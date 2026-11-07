"""Task queue, atomic leases, fencing, terminal replay, and recovery."""

from __future__ import annotations

import hmac
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_after, utc_now
from src.errors import fail, forbidden, invalid_transition, not_found
from src.security.tokens import Principal, new_lease_token, sha256_hex
from src.services.common import (
    all_rows,
    canonical_json,
    emit_event,
    fetch_task,
    fingerprint,
    insert_audit,
    new_id,
    one,
    record_task_event,
    require_agent_project,
    require_human_project,
    resolve_project,
    task_dict,
)

LEASED_STATUSES = ("claimed", "in_progress")
TERMINAL_KIND = {"completed": "complete", "failed": "fail", "cancelled": "cancel"}
TASK_TRANSITIONS = {
    "pending": {"claimed", "cancelled"},
    "claimed": {"in_progress", "completed", "pending", "failed", "blocked", "cancelled"},
    "in_progress": {"completed", "pending", "failed", "blocked", "cancelled"},
    "blocked": {"pending", "cancelled"},
    "failed": {"pending"},
    "completed": set(),
    "cancelled": set(),
}


class TaskService:
    def __init__(self, database: Database | None = None, settings: Settings | None = None):
        self.db = database or default_db
        self.settings = settings or get_settings()

    def retry_delay(self, attempt_count: int) -> int:
        exponent = max(0, attempt_count - 1)
        return min(
            self.settings.task_retry_backoff_seconds * (2**exponent),
            self.settings.task_retry_backoff_max_seconds,
        )

    async def create_task(
        self,
        principal: Principal,
        project_ref: str,
        task_type: str,
        title: str,
        description: str | None = None,
        input_payload: dict[str, Any] | None = None,
        assigned_agent_id: str | None = None,
        target_capability: str | None = None,
        priority: int = 0,
        available_at: str | None = None,
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        try:
            payload_json = canonical_json(input_payload) if input_payload is not None else None
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise fail("validation_error", "Task payload must be valid JSON text.", 422) from exc
        now = utc_now()
        available = available_at or now
        attempts = max_attempts or self.settings.task_max_attempts
        task_id = new_id()
        async with self.db.write_tx() as tx:
            project = await resolve_project(tx, project_ref)
            await require_agent_project(tx, project["id"], principal, ("contributor", "maintainer"))
            try:
                request_fp = fingerprint(
                    {
                        "project": project["id"],
                        "task_type": task_type,
                        "title": title,
                        "description": description,
                        "input_payload": input_payload,
                        "assigned_agent_id": assigned_agent_id,
                        "target_capability": target_capability,
                        "priority": priority,
                        "available_at": available_at,
                        "max_attempts": attempts,
                    }
                )
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise fail(
                    "validation_error", "Task request contains invalid JSON text.", 422
                ) from exc
            if idempotency_key:
                existing = await one(
                    tx,
                    "SELECT id,request_fingerprint FROM tasks WHERE project_id=:project AND idempotency_key=:key",
                    {"project": project["id"], "key": idempotency_key},
                )
                if existing:
                    if existing["request_fingerprint"] != request_fp:
                        raise fail(
                            "idempotency_conflict",
                            "Idempotency key was used for a different task.",
                            409,
                        )
                    existing_task = await fetch_task(tx, existing["id"])
                    return {"task": task_dict(existing_task), "idempotent_replay": True}
            if payload_json:
                try:
                    payload_bytes = len(payload_json.encode("utf-8", errors="strict"))
                except UnicodeEncodeError as exc:
                    raise fail(
                        "validation_error", "Task payload must be valid UTF-8 text.", 422
                    ) from exc
                if payload_bytes > self.settings.max_task_payload_bytes:
                    raise fail(
                        "payload_too_large", "Task payload exceeds MAX_TASK_PAYLOAD_BYTES.", 413
                    )
            if assigned_agent_id:
                member = await one(
                    tx,
                    "SELECT 1 AS yes FROM project_agents WHERE project_id=:project AND agent_id=:agent",
                    {"project": project["id"], "agent": assigned_agent_id},
                )
                if not member:
                    raise fail(
                        "validation_error", "assigned_agent_id must be a project member.", 422
                    )
            await tx.execute(
                text("""INSERT INTO tasks(id,project_id,created_by_agent_id,assigned_agent_id,target_capability,
                           task_type,title,description,input_payload,status,priority,idempotency_key,request_fingerprint,
                           max_attempts,available_at,created_at,updated_at)
                        VALUES(:id,:project,:creator,:assigned,:capability,:type,:title,:description,:payload,
                           'pending',:priority,:key,:fingerprint,:attempts,:available,:now,:now)"""),
                {
                    "id": task_id,
                    "project": project["id"],
                    "creator": principal.id,
                    "assigned": assigned_agent_id,
                    "capability": target_capability,
                    "type": task_type,
                    "title": title,
                    "description": description,
                    "payload": payload_json,
                    "priority": priority,
                    "key": idempotency_key,
                    "fingerprint": request_fp,
                    "attempts": attempts,
                    "available": available,
                    "now": now,
                },
            )
            await record_task_event(
                tx, task_id, principal.id, "created", {"status": "pending"}, now=now
            )
            await emit_event(
                tx,
                "task.created",
                "task",
                task_id,
                project["id"],
                {"task_id": task_id, "status": "pending"},
                now=now,
            )
            if available <= now:
                await emit_event(
                    tx,
                    "task.available",
                    "task",
                    task_id,
                    project["id"],
                    {"task_id": task_id, "status": "pending"},
                    now=now,
                )
            row = await fetch_task(tx, task_id)
        return {"task": task_dict(row), "idempotent_replay": False}

    async def get_task(self, principal: Principal, task_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = await fetch_task(conn, task_id)
            if principal.is_agent:
                await require_agent_project(conn, row["project_id"], principal)
            else:
                await require_human_project(conn, row["project_id"], principal)
        return {"task": task_dict(row)}

    async def get_pending_tasks(
        self,
        principal: Principal,
        project_ref: str,
        *,
        claimable_only: bool = True,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters = {"project": project_ref, "claimable_only": claimable_only}
        after: list[Any] | None = None
        from src.services.common import decode_cursor, encode_cursor

        if cursor:
            after = decode_cursor(self.settings.app_secret, cursor, filters)
        async with self.db.engine.connect() as conn:
            project = await resolve_project(conn, project_ref)
            if principal.is_agent:
                role = await require_agent_project(conn, project["id"], principal)
            else:
                role = await require_human_project(conn, project["id"], principal)
            where = ["t.project_id=:project"]
            params: dict[str, Any] = {"project": project["id"], "limit": limit + 1}
            if claimable_only:
                if not principal.is_agent or role == "reader":
                    return {"tasks": [], "next_cursor": None}
                where.extend(
                    [
                        "t.status='pending'",
                        "t.available_at<=:now",
                        "t.cancel_requested_at IS NULL",
                        "(t.assigned_agent_id IS NULL OR t.assigned_agent_id=:agent)",
                        "(t.target_capability IS NULL OR EXISTS (SELECT 1 FROM json_each((SELECT capabilities FROM agents WHERE id=:agent)) c WHERE c.value=t.target_capability))",
                    ]
                )
                params.update({"agent": principal.id, "now": utc_now()})
            if after:
                where.append(
                    "(t.priority<:priority OR (t.priority=:priority AND (t.created_at>:created OR (t.created_at=:created AND t.id>:id))))"
                )
                params.update({"priority": after[0], "created": after[1], "id": after[2]})
            rows = await all_rows(
                conn,
                f"""SELECT t.*,p.slug AS project_slug FROM tasks t JOIN projects p ON p.id=t.project_id
                   WHERE {" AND ".join(where)} ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT :limit""",
                params,
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(
                self.settings.app_secret,
                filters,
                [page[-1]["priority"], page[-1]["created_at"], page[-1]["id"]],
            )
            if has_more and page
            else None
        )
        return {"tasks": [self._task_summary(row) for row in page], "next_cursor": next_cursor}

    async def claim_task(
        self,
        principal: Principal,
        project_ref: str,
        task_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        async with self.db.write_tx() as tx:
            now = utc_now()
            project = await resolve_project(tx, project_ref)
            await require_agent_project(tx, project["id"], principal, ("contributor", "maintainer"))
            selected = await one(
                tx,
                """SELECT t.id FROM tasks t WHERE t.project_id=:project AND t.status='pending'
                     AND t.available_at<=:now AND t.cancel_requested_at IS NULL
                     AND (t.assigned_agent_id IS NULL OR t.assigned_agent_id=:agent)
                     AND (t.target_capability IS NULL OR EXISTS (
                       SELECT 1 FROM agents a,json_each(a.capabilities) c
                       WHERE a.id=:agent AND c.value=t.target_capability))
                     AND (:task_id IS NULL OR t.id=:task_id)
                   ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT 1""",
                {"project": project["id"], "now": now, "agent": principal.id, "task_id": task_id},
            )
            if selected is None:
                if task_id:
                    # Hide tasks outside the caller's project; otherwise report the requested conflict.
                    visible = await one(
                        tx,
                        "SELECT id FROM tasks WHERE id=:id AND project_id=:project",
                        {"id": task_id, "project": project["id"]},
                    )
                    if not visible:
                        raise not_found()
                    raise fail("task_not_claimable", "The task is not currently claimable.", 409)
                return None
            task_id = selected["id"]
            lease_expires = utc_after(self.settings.task_lease_seconds, now)
            lease_token, token_hash = new_lease_token()
            self._require_transition("pending", "claimed")
            result = await tx.execute(
                text("""UPDATE tasks SET status='claimed',lease_owner_agent_id=:agent,lease_expires_at=:expires,
                           lease_token_hash=:hash,lease_instance_id=:instance,claimed_at=:now,heartbeat_at=:now,
                           attempt_count=attempt_count+1,started_at=COALESCE(started_at,:now),blocked_reason=NULL,
                           updated_at=:now WHERE id=:id AND status='pending'"""),
                {
                    "agent": principal.id,
                    "expires": lease_expires,
                    "hash": token_hash,
                    "instance": instance_id,
                    "now": now,
                    "id": task_id,
                },
            )
            if result.rowcount != 1:
                return None
            await record_task_event(
                tx, task_id, principal.id, "claimed", {"lease_fp": token_hash[:8]}, now=now
            )
            await emit_event(
                tx,
                "task.claimed",
                "task",
                task_id,
                project["id"],
                {"task_id": task_id, "status": "claimed"},
                now=now,
            )
            claimed_task_id = task_id
        async with self.db.engine.connect() as conn:
            row = await fetch_task(conn, claimed_task_id)
            context = await self._claim_context(conn, row)
        task = task_dict(row)
        task["lease"] = {
            "lease_token": lease_token,
            "lease_expires_at": lease_expires,
            "heartbeat_interval_seconds": self.settings.task_heartbeat_seconds,
        }
        task["context"] = context
        return {"task": task}

    async def _claim_context(self, conn: AsyncConnection, task: dict[str, Any]) -> dict[str, Any]:
        project = await one(
            conn,
            "SELECT slug,name,instructions FROM projects WHERE id=:id",
            {"id": task["project_id"]},
        )
        linked = await all_rows(
            conn,
            """SELECT a.id,a.title,a.artifact_type,a.document_key,a.version,a.publication_status,a.file_path,a.commit_sha
               FROM artifacts a WHERE a.project_id=:project AND a.publication_status='published'
               AND (a.task_id=:task OR a.id IN (
                 SELECT latest.id FROM artifacts latest WHERE latest.project_id=:project
                   AND latest.publication_status='published' AND latest.id=(SELECT newer.id FROM artifacts newer
                     WHERE newer.project_id=latest.project_id AND newer.artifact_type=latest.artifact_type
                       AND newer.document_key=latest.document_key AND newer.publication_status='published'
                     ORDER BY newer.version DESC LIMIT 1) ORDER BY latest.published_at DESC LIMIT 10))
               ORDER BY a.published_at DESC LIMIT 20""",
            {"project": task["project_id"], "task": task["id"]},
        )
        return {"project": project, "artifacts": linked}

    async def heartbeat_task(
        self, principal: Principal, task_id: str, lease_token: str, progress_note: str | None = None
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("not_lease_owner")
        now = utc_now()
        token_hash = sha256_hex(lease_token)
        async with self.db.write_tx() as tx:
            task = await fetch_task(tx, task_id)
            await require_agent_project(
                tx, task["project_id"], principal, ("contributor", "maintainer")
            )
            await self._require_lease(tx, task, principal, token_hash)
            max_deadline = utc_after(
                self.settings.task_max_runtime_seconds, task["claimed_at"] or now
            )
            expiry = min(utc_after(self.settings.task_lease_seconds, now), max_deadline)
            await tx.execute(
                text(
                    "UPDATE tasks SET lease_expires_at=:expires,heartbeat_at=:now,updated_at=:now WHERE id=:id"
                ),
                {"expires": expiry, "now": now, "id": task_id},
            )
            await record_task_event(
                tx,
                task_id,
                principal.id,
                "heartbeat",
                {"lease_fp": token_hash[:8]},
                now=now,
            )
            await emit_event(
                tx,
                "task.updated",
                "task",
                task_id,
                task["project_id"],
                {"task_id": task_id, "status": task["status"]},
                now=now,
            )
            row = await fetch_task(tx, task_id)
        return {
            "lease_expires_at": expiry,
            "cancel_requested": bool(row["cancel_requested_at"]),
            "max_runtime_deadline": max_deadline,
        }

    async def update_task(
        self,
        principal: Principal,
        task_id: str,
        *,
        lease_token: str | None = None,
        status: str | None = None,
        progress_note: str | None = None,
        blocked_reason: str | None = None,
        error_detail: str | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        async with self.db.write_tx() as tx:
            task = await fetch_task(tx, task_id)
            # A blocked task is unblocked by its creator or a project maintainer, not by a lease.
            if status == "pending" and task["status"] == "blocked":
                await self._authorize_unblock(tx, task, principal)
                self._require_transition("blocked", "pending")
                await tx.execute(
                    text("""UPDATE tasks SET status='pending',available_at=:now,blocked_reason=NULL,
                           updated_at=:now WHERE id=:id AND status='blocked'"""),
                    {"now": now, "id": task_id},
                )
                await record_task_event(
                    tx,
                    task_id,
                    principal.id if principal.is_agent else None,
                    "unblocked",
                    {"status": "pending"},
                    now=now,
                    actor=principal,
                )
                await emit_event(
                    tx,
                    "task.available",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": "pending"},
                    now=now,
                )
                row = await fetch_task(tx, task_id)
                return {
                    "task": task_dict(row),
                    "cancel_requested": False,
                    "idempotent_replay": False,
                }

            if not principal.is_agent:
                raise forbidden("not_lease_owner")
            token_hash = sha256_hex(lease_token or "")
            await require_agent_project(
                tx, task["project_id"], principal, ("contributor", "maintainer")
            )
            # Terminal owner reports are checked before the current-lease check for replay.
            if status in {"failed", "cancelled"}:
                kind = "fail" if status == "failed" else "cancel"
                report = (
                    {
                        "kind": "fail",
                        "error_detail": error_detail or "",
                        "retryable": bool(retryable),
                    }
                    if kind == "fail"
                    else {"kind": "cancel"}
                )
                report_fp = fingerprint(report)
                replay = await self._terminal_replay(
                    tx, task, principal.id, token_hash, kind, report_fp
                )
                if replay:
                    return {
                        "task": task_dict(task),
                        "cancel_requested": bool(task["cancel_requested_at"]),
                        "idempotent_replay": True,
                    }
            await self._require_lease(tx, task, principal, token_hash)
            if status is None:
                if progress_note is not None:
                    await record_task_event(
                        tx,
                        task_id,
                        principal.id,
                        "progress",
                        {"lease_fp": token_hash[:8]},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        "task.updated",
                        "task",
                        task_id,
                        task["project_id"],
                        {"task_id": task_id, "status": task["status"]},
                        now=now,
                    )
                return {
                    "task": task_dict(task),
                    "cancel_requested": bool(task["cancel_requested_at"]),
                    "idempotent_replay": False,
                }
            if status == "in_progress":
                if task["status"] not in {"claimed", "in_progress"}:
                    raise invalid_transition()
                if task["status"] != "in_progress":
                    self._require_transition(task["status"], "in_progress")
                await tx.execute(
                    text("UPDATE tasks SET status='in_progress',updated_at=:now WHERE id=:id"),
                    {"now": now, "id": task_id},
                )
                await record_task_event(
                    tx,
                    task_id,
                    principal.id,
                    "in_progress",
                    {"lease_fp": token_hash[:8]},
                    now=now,
                )
                await emit_event(
                    tx,
                    "task.updated",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": "in_progress"},
                    now=now,
                )
            elif status == "blocked":
                if task["status"] not in LEASED_STATUSES:
                    raise invalid_transition()
                self._require_transition(task["status"], "blocked")
                await tx.execute(
                    text("""UPDATE tasks SET status='blocked',blocked_reason=:reason,lease_owner_agent_id=NULL,
                           lease_expires_at=NULL,lease_token_hash=NULL,lease_instance_id=NULL,updated_at=:now WHERE id=:id"""),
                    {"reason": blocked_reason, "now": now, "id": task_id},
                )
                await record_task_event(
                    tx,
                    task_id,
                    principal.id,
                    "blocked",
                    {"lease_fp": token_hash[:8]},
                    now=now,
                )
                await emit_event(
                    tx,
                    "task.updated",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": "blocked"},
                    now=now,
                )
            elif status == "pending":
                if task["status"] not in LEASED_STATUSES:
                    raise invalid_transition()
                if task["attempt_count"] >= task["max_attempts"]:
                    self._require_transition(task["status"], "failed")
                    await tx.execute(
                        text("""UPDATE tasks SET status='failed',error_detail='attempts exhausted after release',
                               completed_at=:now,lease_owner_agent_id=NULL,lease_expires_at=NULL,
                               lease_token_hash=NULL,lease_instance_id=NULL,updated_at=:now WHERE id=:id"""),
                        {"now": now, "id": task_id},
                    )
                    await record_task_event(
                        tx,
                        task_id,
                        principal.id,
                        "failed",
                        {"reason": "attempts_exhausted", "lease_fp": token_hash[:8]},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        "task.failed",
                        "task",
                        task_id,
                        task["project_id"],
                        {"task_id": task_id, "status": "failed"},
                        now=now,
                    )
                else:
                    self._require_transition(task["status"], "pending")
                    available = utc_after(self.retry_delay(task["attempt_count"]), now)
                    await self._release_to_pending(tx, task_id, now, available)
                    await record_task_event(
                        tx,
                        task_id,
                        principal.id,
                        "released",
                        {"available_at": available, "lease_fp": token_hash[:8]},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        "task.available",
                        "task",
                        task_id,
                        task["project_id"],
                        {"task_id": task_id, "status": "pending"},
                        now=now,
                    )
            elif status == "failed":
                if task["status"] not in LEASED_STATUSES:
                    raise invalid_transition()
                report = {
                    "kind": "fail",
                    "error_detail": error_detail or "",
                    "retryable": bool(retryable),
                }
                report_fp = fingerprint(report)
                if retryable and task["attempt_count"] < task["max_attempts"]:
                    self._require_transition(task["status"], "pending")
                    available = utc_after(self.retry_delay(task["attempt_count"]), now)
                    await tx.execute(
                        text("""UPDATE tasks SET status='pending',error_detail=:error,available_at=:available,
                               lease_owner_agent_id=NULL,lease_expires_at=NULL,lease_token_hash=NULL,
                               lease_instance_id=NULL,updated_at=:now WHERE id=:id"""),
                        {"error": error_detail, "available": available, "now": now, "id": task_id},
                    )
                    await record_task_event(
                        tx,
                        task_id,
                        principal.id,
                        "retry_scheduled",
                        {"available_at": available, "lease_fp": token_hash[:8]},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        "task.available",
                        "task",
                        task_id,
                        task["project_id"],
                        {"task_id": task_id, "status": "pending"},
                        now=now,
                    )
                else:
                    self._require_transition(task["status"], "failed")
                    await tx.execute(
                        text("""UPDATE tasks SET status='failed',error_detail=:error,completed_at=:now,
                               lease_owner_agent_id=NULL,lease_expires_at=NULL,lease_token_hash=NULL,
                               lease_instance_id=NULL,final_agent_id=:agent,final_lease_token_hash=:token_hash,
                               final_fingerprint=:fingerprint,updated_at=:now WHERE id=:id"""),
                        {
                            "error": error_detail,
                            "now": now,
                            "id": task_id,
                            "agent": principal.id,
                            "token_hash": token_hash,
                            "fingerprint": report_fp,
                        },
                    )
                    await record_task_event(
                        tx, task_id, principal.id, "failed", {"lease_fp": token_hash[:8]}, now=now
                    )
                    await emit_event(
                        tx,
                        "task.failed",
                        "task",
                        task_id,
                        task["project_id"],
                        {"task_id": task_id, "status": "failed"},
                        now=now,
                    )
            elif status == "cancelled":
                if task["status"] not in LEASED_STATUSES or not task["cancel_requested_at"]:
                    raise invalid_transition(
                        "A lease owner may acknowledge only a requested cancellation."
                    )
                self._require_transition(task["status"], "cancelled")
                report_fp = fingerprint({"kind": "cancel"})
                await tx.execute(
                    text("""UPDATE tasks SET status='cancelled',completed_at=:now,lease_owner_agent_id=NULL,
                           lease_expires_at=NULL,lease_token_hash=NULL,lease_instance_id=NULL,
                           final_agent_id=:agent,final_lease_token_hash=:token_hash,final_fingerprint=:fingerprint,
                           updated_at=:now WHERE id=:id"""),
                    {
                        "now": now,
                        "id": task_id,
                        "agent": principal.id,
                        "token_hash": token_hash,
                        "fingerprint": report_fp,
                    },
                )
                await record_task_event(
                    tx, task_id, principal.id, "cancelled", {"lease_fp": token_hash[:8]}, now=now
                )
                await emit_event(
                    tx,
                    "task.cancelled",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": "cancelled"},
                    now=now,
                )
            else:
                raise invalid_transition()
            row = await fetch_task(tx, task_id)
        return {
            "task": task_dict(row),
            "cancel_requested": bool(row["cancel_requested_at"]),
            "idempotent_replay": False,
        }

    async def complete_task(
        self,
        principal: Principal,
        task_id: str,
        lease_token: str,
        result_summary: str,
        artifact_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("not_lease_owner")
        ids = sorted(set(artifact_ids or []))
        token_hash = sha256_hex(lease_token)
        report_fp = fingerprint(
            {"kind": "complete", "result_summary": result_summary, "artifact_ids": ids}
        )
        now = utc_now()
        async with self.db.write_tx() as tx:
            task = await fetch_task(tx, task_id)
            await require_agent_project(
                tx, task["project_id"], principal, ("contributor", "maintainer")
            )
            replay = await self._terminal_replay(
                tx, task, principal.id, token_hash, "complete", report_fp
            )
            if replay:
                return {"task": task_dict(task), "idempotent_replay": True}
            await self._require_lease(tx, task, principal, token_hash)
            if task["status"] not in LEASED_STATUSES:
                raise invalid_transition()
            self._require_transition(task["status"], "completed")
            if ids:
                marks = ",".join(f":id{i}" for i in range(len(ids)))
                params: dict[str, Any] = {f"id{i}": item for i, item in enumerate(ids)}
                params.update({"project": task["project_id"], "task": task_id})
                rows = await all_rows(
                    tx,
                    f"SELECT id FROM artifacts WHERE id IN ({marks}) AND project_id=:project AND task_id=:task",
                    params,
                )
                if len(rows) != len(ids):
                    raise fail(
                        "validation_error",
                        "Every artifact must belong to this project and task.",
                        422,
                    )
            await tx.execute(
                text("""UPDATE tasks SET status='completed',result_summary=:summary,completed_at=:now,
                       lease_owner_agent_id=NULL,lease_expires_at=NULL,lease_token_hash=NULL,lease_instance_id=NULL,
                       final_agent_id=:agent,final_lease_token_hash=:token_hash,final_fingerprint=:fingerprint,
                       updated_at=:now WHERE id=:id"""),
                {
                    "summary": result_summary,
                    "now": now,
                    "id": task_id,
                    "agent": principal.id,
                    "token_hash": token_hash,
                    "fingerprint": report_fp,
                },
            )
            await record_task_event(
                tx,
                task_id,
                principal.id,
                "completed",
                {"lease_fp": token_hash[:8], "artifact_ids": ids},
                now=now,
            )
            await emit_event(
                tx,
                "task.completed",
                "task",
                task_id,
                task["project_id"],
                {"task_id": task_id, "status": "completed"},
                now=now,
            )
            row = await fetch_task(tx, task_id)
        return {"task": task_dict(row), "idempotent_replay": False}

    async def cancel_task(self, principal: Principal, task_id: str) -> dict[str, Any]:
        now = utc_now()
        async with self.db.write_tx() as tx:
            task = await fetch_task(tx, task_id)
            role = await self._task_permission(tx, task, principal)
            if principal.is_agent:
                if role not in {"maintainer"} and task["created_by_agent_id"] != principal.id:
                    raise forbidden("insufficient_role")
            elif role != "maintainer":
                raise forbidden("insufficient_role")
            if task["status"] in {"completed", "failed", "cancelled"}:
                raise invalid_transition()
            if task["status"] in {"pending", "blocked"}:
                self._require_transition(task["status"], "cancelled")
                await tx.execute(
                    text(
                        "UPDATE tasks SET status='cancelled',completed_at=:now,updated_at=:now WHERE id=:id"
                    ),
                    {"now": now, "id": task_id},
                )
                await record_task_event(
                    tx,
                    task_id,
                    principal.id if principal.is_agent else None,
                    "cancelled",
                    {"status": "cancelled"},
                    now=now,
                    actor=principal,
                )
                await emit_event(
                    tx,
                    "task.cancelled",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": "cancelled"},
                    now=now,
                )
            elif task["status"] in LEASED_STATUSES:
                await tx.execute(
                    text(
                        "UPDATE tasks SET cancel_requested_at=COALESCE(cancel_requested_at,:now),updated_at=:now WHERE id=:id"
                    ),
                    {"now": now, "id": task_id},
                )
                await record_task_event(
                    tx,
                    task_id,
                    principal.id if principal.is_agent else None,
                    "cancel_requested",
                    {},
                    now=now,
                    actor=principal,
                )
                await emit_event(
                    tx,
                    "task.cancel_requested",
                    "task",
                    task_id,
                    task["project_id"],
                    {"task_id": task_id, "status": task["status"]},
                    now=now,
                )
            row = await fetch_task(tx, task_id)
        return {"task": task_dict(row)}

    async def retry_task(self, principal: Principal, task_id: str) -> dict[str, Any]:
        now = utc_now()
        async with self.db.write_tx() as tx:
            task = await fetch_task(tx, task_id)
            role = await self._task_permission(tx, task, principal)
            if role != "maintainer":
                raise forbidden("insufficient_role")
            if task["status"] != "failed":
                raise invalid_transition("Only failed tasks can be retried.")
            self._require_transition("failed", "pending")
            await tx.execute(
                text("""UPDATE tasks SET status='pending',attempt_count=0,available_at=:now,error_detail=NULL,
                       completed_at=NULL,final_agent_id=NULL,final_lease_token_hash=NULL,final_fingerprint=NULL,
                       updated_at=:now WHERE id=:id"""),
                {"now": now, "id": task_id},
            )
            await record_task_event(
                tx,
                task_id,
                principal.id if principal.is_agent else None,
                "retried",
                {"status": "pending"},
                now=now,
                actor=principal,
            )
            await emit_event(
                tx,
                "task.available",
                "task",
                task_id,
                task["project_id"],
                {"task_id": task_id, "status": "pending"},
                now=now,
            )
            row = await fetch_task(tx, task_id)
        return {"task": task_dict(row)}

    async def task_events(self, principal: Principal, task_id: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            task = await fetch_task(conn, task_id)
            if principal.is_agent:
                await require_agent_project(conn, task["project_id"], principal)
            else:
                await require_human_project(conn, task["project_id"], principal)
            rows = await all_rows(
                conn,
                "SELECT * FROM task_events WHERE task_id=:task ORDER BY created_at,id LIMIT 500",
                {"task": task_id},
            )
        for row in rows:
            row["payload"] = json.loads(row["payload"]) if row["payload"] else None
        return rows

    async def reap_expired(self, batch_size: int = 50) -> int:
        changed = 0
        batch_limit = min(max(batch_size, 1), 50)
        while True:
            now = utc_now()
            async with self.db.write_tx() as tx:
                rows = await all_rows(
                    tx,
                    """SELECT * FROM tasks WHERE status IN ('claimed','in_progress') AND lease_expires_at<:now
                       ORDER BY lease_expires_at LIMIT :limit""",
                    {"now": now, "limit": batch_limit},
                )
                if not rows:
                    return changed
                for task in rows:
                    if task["cancel_requested_at"]:
                        status, event_type, available, error = (
                            "cancelled",
                            "task.cancelled",
                            None,
                            None,
                        )
                    elif task["attempt_count"] >= task["max_attempts"]:
                        status, event_type, available, error = (
                            "failed",
                            "task.failed",
                            None,
                            "lease expired; attempts exhausted",
                        )
                    else:
                        status, event_type, error = "pending", "task.available", None
                        available = utc_after(self.retry_delay(task["attempt_count"]), now)
                    self._require_transition(task["status"], status)
                    await tx.execute(
                        text("""UPDATE tasks SET status=:status,available_at=COALESCE(:available,available_at),
                               error_detail=COALESCE(:error,error_detail),completed_at=CASE WHEN :terminal=1 THEN :now ELSE completed_at END,
                               lease_owner_agent_id=NULL,lease_expires_at=NULL,lease_token_hash=NULL,lease_instance_id=NULL,
                               updated_at=:now WHERE id=:id"""),
                        {
                            "status": status,
                            "available": available,
                            "error": error,
                            "terminal": int(status in {"failed", "cancelled"}),
                            "now": now,
                            "id": task["id"],
                        },
                    )
                    await record_task_event(
                        tx,
                        task["id"],
                        None,
                        "lease_expired",
                        {"previous_status": task["status"], "status": status},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        "task.lease_expired",
                        "task",
                        task["id"],
                        task["project_id"],
                        {"task_id": task["id"], "status": status},
                        now=now,
                    )
                    await emit_event(
                        tx,
                        event_type,
                        "task",
                        task["id"],
                        task["project_id"],
                        {"task_id": task["id"], "status": status},
                        now=now,
                    )
                changed += len(rows)
            if len(rows) < batch_limit:
                return changed

    async def _require_lease(
        self, conn: AsyncConnection, task: dict[str, Any], principal: Principal, token_hash: str
    ) -> None:
        valid = (
            task["status"] in LEASED_STATUSES
            and task["lease_owner_agent_id"] == principal.id
            and task["lease_token_hash"] is not None
            and hmac.compare_digest(task["lease_token_hash"], token_hash)
        )
        if valid:
            return
        await self._audit_stale_rejection(conn, task, principal)
        raise forbidden("not_lease_owner")

    async def _audit_stale_rejection(
        self, conn: AsyncConnection, task: dict[str, Any], principal: Principal
    ) -> None:
        cutoff = utc_after(-60)
        recent = await one(
            conn,
            """SELECT id FROM audit_logs WHERE actor_type='agent' AND actor_id=:agent
               AND action='task.stale_lease_rejected' AND resource_type='task' AND resource_id=:task
               AND created_at>:cutoff LIMIT 1""",
            {"agent": principal.id, "task": task["id"], "cutoff": cutoff},
        )
        if not recent:
            await insert_audit(
                conn,
                actor_type="agent",
                actor_id=principal.id,
                action="task.stale_lease_rejected",
                resource_type="task",
                resource_id=task["id"],
                project_id=task["project_id"],
            )

    async def _terminal_replay(
        self,
        conn: AsyncConnection,
        task: dict[str, Any],
        agent_id: str,
        token_hash: str,
        kind: str,
        report_fp: str,
    ) -> bool:
        if not task["final_fingerprint"]:
            return False
        if (
            task["final_agent_id"] != agent_id
            or task["final_lease_token_hash"] is None
            or not hmac.compare_digest(task["final_lease_token_hash"], token_hash)
        ):
            return False
        old_kind = TERMINAL_KIND.get(task["status"])
        if old_kind != kind:
            raise invalid_transition("A different terminal report already completed this task.")
        if not hmac.compare_digest(task["final_fingerprint"], report_fp):
            raise fail(
                "completion_conflict", "A different terminal report was already recorded.", 409
            )
        return True

    async def _authorize_unblock(
        self, conn: AsyncConnection, task: dict[str, Any], principal: Principal
    ) -> None:
        if principal.is_agent:
            role = await require_agent_project(conn, task["project_id"], principal)
            if role != "maintainer" and task["created_by_agent_id"] != principal.id:
                raise forbidden("insufficient_role")
        else:
            role = await require_human_project(conn, task["project_id"], principal)
            if role != "maintainer":
                raise forbidden("insufficient_role")

    async def _task_permission(
        self, conn: AsyncConnection, task: dict[str, Any], principal: Principal
    ) -> str:
        if principal.is_agent:
            return await require_agent_project(conn, task["project_id"], principal)
        return await require_human_project(conn, task["project_id"], principal)

    async def _release_to_pending(
        self, conn: AsyncConnection, task_id: str, now: str, available: str
    ) -> None:
        await conn.execute(
            text("""UPDATE tasks SET status='pending',available_at=:available,lease_owner_agent_id=NULL,
                   lease_expires_at=NULL,lease_token_hash=NULL,lease_instance_id=NULL,updated_at=:now WHERE id=:id"""),
            {"available": available, "now": now, "id": task_id},
        )

    @staticmethod
    def _require_transition(current: str, target: str) -> None:
        if target not in TASK_TRANSITIONS.get(current, set()):
            raise invalid_transition()

    @staticmethod
    def _task_summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "task_type",
                "title",
                "status",
                "priority",
                "assigned_agent_id",
                "target_capability",
                "attempt_count",
                "available_at",
                "created_at",
                "updated_at",
            )
        } | {"project": row["project_slug"]}


task_service = TaskService()
