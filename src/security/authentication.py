"""Bearer-token and browser-session authentication helpers."""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import text

from src.database.connection import Database
from src.database.types import utc_after, utc_now
from src.security.tokens import Principal, safe_compare, sha256_hex

AGENT_TOKEN_RE = re.compile(r"^ah_([A-Za-z0-9_-]{8})_([A-Za-z0-9_-]{32,})$")


async def authenticate_agent(db: Database, raw_token: str) -> Principal | None:
    match = AGENT_TOKEN_RE.fullmatch(raw_token)
    if not match:
        return None
    prefix = match.group(1)
    now = utc_now()
    async with db.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""SELECT t.id AS token_id,t.token_hash,t.expires_at,t.revoked_at,t.last_used_at,
                          a.id,a.name,a.status,a.last_seen_at
                   FROM agent_tokens t JOIN agents a ON a.id=t.agent_id WHERE t.token_prefix=:prefix"""),
                    {"prefix": prefix},
                )
            )
            .mappings()
            .all()
        )
    digest = sha256_hex(raw_token)
    row = next((r for r in rows if safe_compare(r["token_hash"], digest)), None)
    if row is None or row["revoked_at"] or row["status"] != "active":
        return None
    if row["expires_at"] and row["expires_at"] <= now:
        return None

    if not row["last_used_at"] or row["last_used_at"] <= utc_after(-60):
        async with db.write_tx() as tx:
            await tx.execute(
                text(
                    "UPDATE agent_tokens SET last_used_at=:now WHERE id=:id AND (last_used_at IS NULL OR last_used_at<=:cutoff)"
                ),
                {"now": now, "id": row["token_id"], "cutoff": utc_after(-60)},
            )
    if not row["last_seen_at"] or row["last_seen_at"] <= utc_after(-30):
        async with db.write_tx() as tx:
            await tx.execute(
                text(
                    "UPDATE agents SET last_seen_at=:now,updated_at=:now WHERE id=:id AND (last_seen_at IS NULL OR last_seen_at<=:cutoff)"
                ),
                {"now": now, "id": row["id"], "cutoff": utc_after(-30)},
            )
    return Principal(kind="agent", id=row["id"], name=row["name"], token_id=row["token_id"])


async def authenticate_session(db: Database, raw_token: str, idle_timeout: int) -> Principal | None:
    if not raw_token or len(raw_token) > 256:
        return None
    session_id = hashlib.sha256(raw_token.encode()).hexdigest()
    now = utc_now()
    async with db.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("""SELECT s.*,u.email,u.display_name,u.role,u.is_active
                   FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=:id"""),
                    {"id": session_id},
                )
            )
            .mappings()
            .first()
        )
        row = dict(row) if row else None
    if (
        row is None
        or row["revoked_at"]
        or not row["is_active"]
        or row["idle_expires_at"] <= now
        or row["absolute_expires_at"] <= now
    ):
        return None
    if not row["last_seen_at"] or row["last_seen_at"] <= utc_after(-60):
        async with db.write_tx() as tx:
            await tx.execute(
                text("""UPDATE sessions SET last_seen_at=:now,idle_expires_at=:idle
                       WHERE id=:id AND revoked_at IS NULL AND idle_expires_at>:now"""),
                {
                    "now": now,
                    "idle": min(utc_after(idle_timeout), row["absolute_expires_at"]),
                    "id": session_id,
                },
            )
    return Principal(
        kind="user",
        id=row["user_id"],
        name=row["display_name"],
        role=row["role"],
        token_id=session_id,
        csrf_secret=row["csrf_secret"],
    )
