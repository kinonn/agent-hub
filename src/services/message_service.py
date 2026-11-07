from __future__ import annotations

from typing import Any

from sqlalchemy import text

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_now
from src.errors import fail, forbidden, not_found
from src.security.tokens import Principal
from src.services.common import (
    all_rows,
    decode_cursor,
    emit_event,
    encode_cursor,
    fingerprint,
    message_dict,
    new_id,
    one,
    require_agent_project,
    require_project_access,
    resolve_project,
)


class MessageService:
    def __init__(self, database: Database | None = None, settings: Settings | None = None):
        self.db = database or default_db
        self.settings = settings or get_settings()

    async def publish_message(
        self,
        principal: Principal,
        project_ref: str,
        subject: str,
        body: str,
        message_type: str,
        recipient_agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        request = {
            "subject": subject,
            "body": body,
            "message_type": message_type,
            "recipient_agent_id": recipient_agent_id,
            "thread_id": thread_id,
        }
        try:
            request_fp = fingerprint(request)
        except UnicodeEncodeError as exc:
            raise fail("validation_error", "Message text must be valid UTF-8.", 422) from exc
        now, message_id = utc_now(), new_id()
        async with self.db.write_tx() as tx:
            project = await resolve_project(tx, project_ref)
            await require_agent_project(tx, project["id"], principal, ("contributor", "maintainer"))
            if idempotency_key:
                existing = await one(
                    tx,
                    """SELECT m.*,p.slug AS project_slug FROM messages m JOIN projects p ON p.id=m.project_id
                       WHERE m.project_id=:project AND m.sender_agent_id=:sender AND m.idempotency_key=:key""",
                    {"project": project["id"], "sender": principal.id, "key": idempotency_key},
                )
                if existing:
                    if existing["request_fingerprint"] != request_fp:
                        raise fail(
                            "idempotency_conflict",
                            "Idempotency key was used for a different message.",
                            409,
                        )
                    return {"message": message_dict(existing), "idempotent_replay": True}
            if message_type == "system":
                raise forbidden("insufficient_role")
            if len(body.encode("utf-8")) > self.settings.max_message_bytes:
                raise fail("payload_too_large", "Message body exceeds MAX_MESSAGE_BYTES.", 413)
            if recipient_agent_id:
                recipient = await one(
                    tx,
                    "SELECT 1 AS yes FROM project_agents WHERE project_id=:project AND agent_id=:agent",
                    {"project": project["id"], "agent": recipient_agent_id},
                )
                if not recipient:
                    raise fail(
                        "validation_error", "Recipient must be a member of this project.", 422
                    )
            root_thread = None
            if thread_id:
                parent = await one(
                    tx,
                    "SELECT id,thread_id,project_id FROM messages WHERE id=:id",
                    {"id": thread_id},
                )
                if not parent or parent["project_id"] != project["id"]:
                    raise not_found()
                root_thread = parent["thread_id"] or parent["id"]
            await tx.execute(
                text("""INSERT INTO messages(id,project_id,sender_agent_id,recipient_agent_id,thread_id,subject,body,
                            message_type,idempotency_key,request_fingerprint,created_at)
                        VALUES(:id,:project,:sender,:recipient,:thread,:subject,:body,:type,:key,:fingerprint,:now)"""),
                {
                    "id": message_id,
                    "project": project["id"],
                    "sender": principal.id,
                    "recipient": recipient_agent_id,
                    "thread": root_thread,
                    "subject": subject,
                    "body": body,
                    "type": message_type,
                    "key": idempotency_key,
                    "fingerprint": request_fp,
                    "now": now,
                },
            )
            if recipient_agent_id:
                await tx.execute(
                    text(
                        "INSERT INTO message_receipts(message_id,agent_id) VALUES(:message,:agent)"
                    ),
                    {"message": message_id, "agent": recipient_agent_id},
                )
            await emit_event(
                tx,
                "message.created",
                "message",
                message_id,
                project["id"],
                {"message_id": message_id},
                now=now,
            )
            message = await one(
                tx,
                "SELECT m.*,p.slug AS project_slug FROM messages m JOIN projects p ON p.id=m.project_id WHERE m.id=:id",
                {"id": message_id},
            )
        return {"message": message_dict(message), "idempotent_replay": False}

    async def get_messages(
        self,
        principal: Principal,
        project_ref: str,
        *,
        unread_only: bool = False,
        thread_id: str | None = None,
        since: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters = {
            "project": project_ref,
            "unread_only": unread_only,
            "thread_id": thread_id,
            "since": since,
        }
        after: list[Any] | None = (
            decode_cursor(self.settings.app_secret, cursor, filters) if cursor else None
        )
        async with self.db.engine.connect() as conn:
            project = await resolve_project(conn, project_ref)
            await require_project_access(conn, project["id"], principal)
            where = ["m.project_id=:project"]
            params: dict[str, Any] = {"project": project["id"], "limit": limit + 1}
            if principal.is_agent:
                params["agent"] = principal.id
            if thread_id:
                where.append("(m.thread_id=:thread OR m.id=:thread)")
                params["thread"] = thread_id
            if since:
                where.append("m.created_at>=:since")
                params["since"] = since
            if unread_only:
                if principal.is_agent:
                    where.append("""(m.sender_agent_id IS NULL OR m.sender_agent_id<>:agent) AND (
                       (m.recipient_agent_id=:agent AND (r.read_at IS NULL)) OR
                       (m.recipient_agent_id IS NULL AND m.created_at>=pa.created_at AND r.read_at IS NULL))""")
                else:
                    where.append("1=0")
            if after:
                where.append(
                    "(m.created_at>:cursor_created OR (m.created_at=:cursor_created AND m.id>:cursor_id))"
                )
                params.update({"cursor_created": after[0], "cursor_id": after[1]})
            join = (
                "LEFT JOIN message_receipts r ON r.message_id=m.id AND r.agent_id=:agent"
                if principal.is_agent
                else "LEFT JOIN message_receipts r ON r.message_id=m.id AND 1=0"
            )
            if principal.is_agent:
                join += " LEFT JOIN project_agents pa ON pa.project_id=m.project_id AND pa.agent_id=:agent"
            rows = await all_rows(
                conn,
                f"""SELECT m.*,p.slug AS project_slug,r.read_at,r.acknowledged_at FROM messages m
                    JOIN projects p ON p.id=m.project_id {join}
                    WHERE {" AND ".join(where)} ORDER BY m.created_at ASC,m.id ASC LIMIT :limit""",
                params,
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(
                self.settings.app_secret, filters, [page[-1]["created_at"], page[-1]["id"]]
            )
            if has_more and page
            else None
        )
        # Marking read is explicit; GET remains side-effect-free.
        return {"messages": [message_dict(row) for row in page], "next_cursor": next_cursor}

    async def mark_message_read(
        self, principal: Principal, message_id: str, acknowledge: bool = False
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        now = utc_now()
        async with self.db.write_tx() as tx:
            message = await one(tx, "SELECT * FROM messages WHERE id=:id", {"id": message_id})
            if message is None:
                raise not_found()
            await require_agent_project(tx, message["project_id"], principal)
            if message["recipient_agent_id"] and message["recipient_agent_id"] != principal.id:
                raise not_found()
            if not message["recipient_agent_id"] and message["sender_agent_id"] == principal.id:
                raise not_found()
            receipt = await one(
                tx,
                "SELECT * FROM message_receipts WHERE message_id=:message AND agent_id=:agent",
                {"message": message_id, "agent": principal.id},
            )
            read_at = receipt["read_at"] if receipt and receipt["read_at"] else now
            acknowledged_at = (
                receipt["acknowledged_at"]
                if receipt and receipt["acknowledged_at"]
                else (now if acknowledge else None)
            )
            await tx.execute(
                text("""INSERT INTO message_receipts(message_id,agent_id,read_at,acknowledged_at)
                        VALUES(:message,:agent,:read,:ack)
                        ON CONFLICT(message_id,agent_id) DO UPDATE SET
                          read_at=COALESCE(message_receipts.read_at,excluded.read_at),
                          acknowledged_at=COALESCE(message_receipts.acknowledged_at,excluded.acknowledged_at)"""),
                {
                    "message": message_id,
                    "agent": principal.id,
                    "read": read_at,
                    "ack": acknowledged_at,
                },
            )
        return {
            "receipt": {
                "message_id": message_id,
                "agent_id": principal.id,
                "read_at": read_at,
                "acknowledged_at": acknowledged_at,
            }
        }


message_service = MessageService()
