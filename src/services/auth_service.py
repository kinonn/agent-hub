"""Human account, browser session, agent, and project provisioning."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from sqlalchemy import text

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_after, utc_now
from src.errors import AgentHubError, fail, forbidden, not_found
from src.security.tokens import Principal, new_agent_token
from src.services.common import all_rows, insert_audit, new_id, one, require_admin

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _contains_secret_config(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized.endswith(("_secret_file", "_secret_name", "_token_file", "_key_file")):
                continue
            if any(
                term in normalized
                for term in (
                    "token",
                    "password",
                    "secret",
                    "private_key",
                    "credential",
                    "authorization",
                )
            ):
                return True
            if _contains_secret_config(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_config(item) for item in value)
    return False


class AuthService:
    def __init__(self, database: Database | None = None, settings: Settings | None = None):
        self.db = database or default_db
        self.settings = settings or get_settings()
        self.login_attempts: dict[str, deque[float]] = defaultdict(deque)

    async def create_user(
        self,
        email: str,
        password: str,
        display_name: str,
        role: str = "member",
        actor: Principal | None = None,
    ) -> dict[str, Any]:
        if actor is not None:
            require_admin(actor)
        if (
            role not in {"admin", "member"}
            or len(password) < 12
            or len(password) > 1024
            or not EMAIL_RE.fullmatch(email.strip())
            or not display_name.strip()
        ):
            raise fail("validation_error", "Invalid email, role, or password length.", 422)
        user_id = new_id()
        now = utc_now()
        password_hash = PASSWORDS.hash(password)
        async with self.db.write_tx() as tx:
            if await one(
                tx,
                "SELECT id FROM users WHERE email=:email COLLATE NOCASE",
                {"email": email.strip()},
            ):
                raise fail("validation_error", "A user with this email already exists.", 422)
            await tx.execute(
                text("""INSERT INTO users(id,email,password_hash,display_name,role,created_at,updated_at)
                            VALUES(:id,:email,:password,:name,:role,:now,:now)"""),
                {
                    "id": user_id,
                    "email": email.strip().lower(),
                    "password": password_hash,
                    "name": display_name.strip(),
                    "role": role,
                    "now": now,
                },
            )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="user.created",
                resource_type="user",
                resource_id=user_id,
                metadata={"role": role},
            )
        return {
            "id": user_id,
            "email": email.strip().lower(),
            "display_name": display_name.strip(),
            "role": role,
        }

    async def create_first_admin(
        self, email: str, password: str, display_name: str | None = None
    ) -> dict[str, Any]:
        if len(password) < 12 or len(password) > 1024:
            raise fail("validation_error", "Password must contain 12 to 1024 characters.", 422)
        if (
            not EMAIL_RE.fullmatch(email.strip())
            or not (display_name or email.split("@", 1)[0]).strip()
        ):
            raise fail("validation_error", "Email address or display name is invalid.", 422)
        user_id = new_id()
        now = utc_now()
        password_hash = PASSWORDS.hash(password)
        async with self.db.write_tx() as tx:
            count = (
                await tx.execute(text("SELECT count(*) FROM users WHERE role='admin'"))
            ).scalar_one()
            if count:
                raise fail("forbidden", "An administrator already exists.", 403)
            if await one(
                tx,
                "SELECT id FROM users WHERE email=:email COLLATE NOCASE",
                {"email": email.strip()},
            ):
                raise fail("validation_error", "A user with this email already exists.", 422)
            await tx.execute(
                text("""INSERT INTO users(id,email,password_hash,display_name,role,created_at,updated_at)
                        VALUES(:id,:email,:password,:name,'admin',:now,:now)"""),
                {
                    "id": user_id,
                    "email": email.strip().lower(),
                    "password": password_hash,
                    "name": (display_name or email.split("@", 1)[0]).strip(),
                    "now": now,
                },
            )
            await insert_audit(
                tx,
                actor_type="system",
                actor_id="system",
                action="user.created",
                resource_type="user",
                resource_id=user_id,
                metadata={"role": "admin"},
            )
        return {
            "id": user_id,
            "email": email.strip().lower(),
            "display_name": (display_name or email.split("@", 1)[0]).strip(),
            "role": "admin",
        }

    async def login(
        self, email: str, password: str, ip_address: str | None, user_agent: str | None
    ) -> dict[str, Any]:
        now_dt = datetime.now(UTC)
        bucket_key = f"{email.strip().lower()}|{ip_address or 'unknown'}"
        bucket = self.login_attempts[bucket_key]
        while bucket and bucket[0] < (now_dt - timedelta(minutes=15)).timestamp():
            bucket.popleft()
        if len(bucket) >= 5:
            raise AgentHubError(
                "rate_limited", "Too many login attempts.", 429, headers={"Retry-After": "900"}
            )
        bucket.append(now_dt.timestamp())

        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM users WHERE email=:email COLLATE NOCASE"),
                        {"email": email.strip()},
                    )
                )
                .mappings()
                .first()
            )
            row = dict(row) if row else None
        invalid = row is None or not row["is_active"]
        if row and row["locked_until"] and row["locked_until"] > utc_now():
            raise AgentHubError(
                "rate_limited", "Account temporarily locked.", 429, headers={"Retry-After": "900"}
            )
        if row and not invalid:
            try:
                PASSWORDS.verify(row["password_hash"], password)
            except Exception:
                invalid = True
        if invalid:
            if row:
                async with self.db.write_tx() as tx:
                    current = await one(
                        tx, "SELECT failed_login_count FROM users WHERE id=:id", {"id": row["id"]}
                    )
                    count = int(current["failed_login_count"] if current else 0) + 1
                    lock_until = utc_after(900) if count >= 5 else None
                    await tx.execute(
                        text(
                            "UPDATE users SET failed_login_count=:count,locked_until=:locked,updated_at=:now WHERE id=:id"
                        ),
                        {"count": count, "locked": lock_until, "now": utc_now(), "id": row["id"]},
                    )
                    await insert_audit(
                        tx,
                        actor_type="user",
                        actor_id=row["id"],
                        action="auth.login_failed",
                        resource_type="user",
                        resource_id=row["id"],
                    )
            raise fail("unauthenticated", "Invalid email or password.", 401)

        raw_session = secrets.token_urlsafe(32)
        session_id = hashlib.sha256(raw_session.encode()).hexdigest()
        csrf = secrets.token_urlsafe(32)
        now = utc_now()
        async with self.db.write_tx() as tx:
            await tx.execute(
                text(
                    """UPDATE users SET failed_login_count=0,locked_until=NULL,last_login_at=:now,updated_at=:now WHERE id=:id"""
                ),
                {"now": now, "id": row["id"]},
            )
            await tx.execute(
                text("""INSERT INTO sessions(id,user_id,csrf_secret,created_at,last_seen_at,idle_expires_at,
                           absolute_expires_at,user_agent,ip_address)
                        VALUES(:id,:user,:csrf,:now,:now,:idle,:absolute,:ua,:ip)"""),
                {
                    "id": session_id,
                    "user": row["id"],
                    "csrf": csrf,
                    "now": now,
                    "idle": utc_after(self.settings.session_idle_timeout_seconds),
                    "absolute": utc_after(self.settings.session_absolute_timeout_seconds),
                    "ua": (user_agent or "")[:512],
                    "ip": ip_address,
                },
            )
            await insert_audit(
                tx,
                actor_type="user",
                actor_id=row["id"],
                action="auth.login",
                resource_type="user",
                resource_id=row["id"],
            )
        bucket.clear()
        return {
            "session_token": raw_session,
            "csrf_token": csrf,
            "user": {
                "id": row["id"],
                "email": row["email"],
                "display_name": row["display_name"],
                "role": row["role"],
            },
        }

    async def logout(self, principal: Principal) -> None:
        if not principal.is_user:
            raise forbidden("insufficient_role")
        async with self.db.write_tx() as tx:
            await tx.execute(
                text(
                    "UPDATE sessions SET revoked_at=:now WHERE id=:session AND user_id=:user AND revoked_at IS NULL"
                ),
                {"now": utc_now(), "session": principal.token_id, "user": principal.id},
            )
            await insert_audit(
                tx,
                actor_type="user",
                actor_id=principal.id,
                action="auth.logout",
                resource_type="user",
                resource_id=principal.id,
            )

    async def create_agent(
        self,
        name: str,
        agent_type: str = "other",
        description: str | None = None,
        capabilities: list[str] | None = None,
        execution_mode: str = "poll",
        adapter_config: dict[str, Any] | None = None,
        actor: Principal | None = None,
    ) -> dict[str, Any]:
        if actor is not None:
            require_admin(actor)
        if agent_type not in {"hermes", "opencode", "copilot", "other"} or execution_mode not in {
            "poll",
            "adapter",
        }:
            raise fail("validation_error", "Invalid agent type or execution mode.", 422)
        if not name.strip() or len(name) > 120:
            raise fail("validation_error", "Agent name must contain 1 to 120 characters.", 422)
        if (
            len(capabilities or []) > 32
            or len(set(capabilities or [])) != len(capabilities or [])
            or any(not CAPABILITY_RE.fullmatch(cap) for cap in (capabilities or []))
        ):
            raise fail(
                "validation_error", "Agent capabilities must be unique lowercase identifiers.", 422
            )
        agent_id = new_id()
        raw, prefix, token_hash = new_agent_token()
        token_id = new_id()
        now = utc_now()
        if _contains_secret_config(adapter_config or {}):
            raise fail("validation_error", "adapter_config may not contain secrets.", 422)

        async with self.db.write_tx() as tx:
            if await one(
                tx, "SELECT id FROM agents WHERE name=:name COLLATE NOCASE", {"name": name.strip()}
            ):
                raise fail("validation_error", "An agent with this name already exists.", 422)
            await tx.execute(
                text("""INSERT INTO agents(id,name,agent_type,description,execution_mode,adapter_config,capabilities,created_at,updated_at)
                        VALUES(:id,:name,:type,:description,:mode,:config,:capabilities,:now,:now)"""),
                {
                    "id": agent_id,
                    "name": name.strip(),
                    "type": agent_type,
                    "description": description,
                    "mode": execution_mode,
                    "config": json.dumps(adapter_config) if adapter_config else None,
                    "capabilities": json.dumps(capabilities or []),
                    "now": now,
                },
            )
            await tx.execute(
                text(
                    "INSERT INTO agent_tokens(id,agent_id,token_hash,token_prefix,created_at) VALUES(:id,:agent,:hash,:prefix,:now)"
                ),
                {
                    "id": token_id,
                    "agent": agent_id,
                    "hash": token_hash,
                    "prefix": prefix,
                    "now": now,
                },
            )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="agent.created",
                resource_type="agent",
                resource_id=agent_id,
                metadata={"agent_type": agent_type},
            )
        return {
            "agent": {
                "id": agent_id,
                "name": name.strip(),
                "agent_type": agent_type,
                "description": description,
                "status": "active",
                "capabilities": capabilities or [],
                "last_seen_at": None,
            },
            "token_id": token_id,
            "token": raw,
        }

    async def rotate_agent_token(
        self, agent_id: str, actor: Principal | None = None
    ) -> dict[str, str]:
        if actor is not None:
            require_admin(actor)
        raw, prefix, token_hash = new_agent_token()
        token_id = new_id()
        now = utc_now()
        async with self.db.write_tx() as tx:
            agent = await one(tx, "SELECT id FROM agents WHERE id=:id", {"id": agent_id})
            if not agent:
                raise not_found()
            await tx.execute(
                text(
                    "UPDATE agent_tokens SET revoked_at=:now WHERE agent_id=:agent AND revoked_at IS NULL"
                ),
                {"now": now, "agent": agent_id},
            )
            await tx.execute(
                text(
                    "INSERT INTO agent_tokens(id,agent_id,token_hash,token_prefix,created_at) VALUES(:id,:agent,:hash,:prefix,:now)"
                ),
                {
                    "id": token_id,
                    "agent": agent_id,
                    "hash": token_hash,
                    "prefix": prefix,
                    "now": now,
                },
            )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="agent.token_rotated",
                resource_type="agent",
                resource_id=agent_id,
            )
        return {"token_id": token_id, "token": raw}

    async def revoke_agent_token(
        self, agent_id: str, token_id: str, actor: Principal | None = None
    ) -> None:
        if actor is not None:
            require_admin(actor)
        async with self.db.write_tx() as tx:
            result = await tx.execute(
                text(
                    "UPDATE agent_tokens SET revoked_at=:now WHERE id=:token AND agent_id=:agent AND revoked_at IS NULL"
                ),
                {"now": utc_now(), "token": token_id, "agent": agent_id},
            )
            if result.rowcount != 1:
                raise not_found()
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="agent.token_revoked",
                resource_type="agent",
                resource_id=agent_id,
                metadata={"token_id": token_id},
            )

    async def set_agent_status(
        self, agent_id: str, status: str, actor: Principal | None = None
    ) -> None:
        if actor is not None:
            require_admin(actor)
        if status not in {"active", "disabled"}:
            raise fail("validation_error", "Invalid agent status.", 422)
        async with self.db.write_tx() as tx:
            result = await tx.execute(
                text("UPDATE agents SET status=:status,updated_at=:now WHERE id=:id"),
                {"status": status, "now": utc_now(), "id": agent_id},
            )
            if result.rowcount != 1:
                raise not_found()
            if status == "disabled":
                await tx.execute(
                    text(
                        "UPDATE agent_tokens SET revoked_at=:now WHERE agent_id=:id AND revoked_at IS NULL"
                    ),
                    {"now": utc_now(), "id": agent_id},
                )
                await tx.execute(
                    text("""UPDATE tasks SET lease_expires_at=:now,updated_at=:now
                                      WHERE lease_owner_agent_id=:id AND status IN ('claimed','in_progress')"""),
                    {"now": utc_now(), "id": agent_id},
                )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action=f"agent.{status}",
                resource_type="agent",
                resource_id=agent_id,
            )

    async def patch_agent(
        self, agent_id: str, fields: dict[str, Any], actor: Principal
    ) -> dict[str, Any]:
        require_admin(actor)
        allowed = {
            "name",
            "agent_type",
            "description",
            "execution_mode",
            "capabilities",
            "adapter_config",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates = {
            key: value
            for key, value in updates.items()
            if value is not None or key in {"description", "adapter_config"}
        }
        if "adapter_config" in updates and updates["adapter_config"]:
            if _contains_secret_config(updates["adapter_config"]):
                raise fail("validation_error", "adapter_config may not contain secrets.", 422)
        if "capabilities" in updates and updates["capabilities"] is not None:
            caps = updates["capabilities"]
            if (
                len(caps) > 32
                or len(set(caps)) != len(caps)
                or any(not CAPABILITY_RE.fullmatch(cap) for cap in caps)
            ):
                raise fail(
                    "validation_error",
                    "Agent capabilities must be unique lowercase identifiers.",
                    422,
                )
        if "name" in updates:
            updates["name"] = updates["name"].strip()
            if not updates["name"]:
                raise fail("validation_error", "Agent name may not be empty.", 422)
        async with self.db.write_tx() as tx:
            agent = await one(tx, "SELECT * FROM agents WHERE id=:id", {"id": agent_id})
            if not agent:
                raise not_found()
            if "name" in updates:
                duplicate = await one(
                    tx,
                    "SELECT id FROM agents WHERE name=:name COLLATE NOCASE AND id<>:id",
                    {"name": updates["name"], "id": agent_id},
                )
                if duplicate:
                    raise fail("validation_error", "An agent with this name already exists.", 422)
            if "status" in fields:
                status = fields["status"]
                if status not in {"active", "disabled"}:
                    raise fail("validation_error", "Invalid agent status.", 422)
                updates["status"] = status
                if status == "disabled":
                    await tx.execute(
                        text(
                            "UPDATE agent_tokens SET revoked_at=:now WHERE agent_id=:id AND revoked_at IS NULL"
                        ),
                        {"now": utc_now(), "id": agent_id},
                    )
                    await tx.execute(
                        text("""UPDATE tasks SET lease_expires_at=:now,updated_at=:now
                                          WHERE lease_owner_agent_id=:id AND status IN ('claimed','in_progress')"""),
                        {"now": utc_now(), "id": agent_id},
                    )
            if updates:
                assignments = []
                params: dict[str, Any] = {"id": agent_id, "now": utc_now()}
                for key, value in updates.items():
                    if key == "capabilities":
                        value = json.dumps(value)
                    elif key == "adapter_config":
                        value = json.dumps(value) if value is not None else None
                    assignments.append(f"{key}=:{key}")
                    params[key] = value
                assignments.append("updated_at=:now")
                await tx.execute(
                    text(f"UPDATE agents SET {','.join(assignments)} WHERE id=:id"), params
                )
            await insert_audit(
                tx,
                actor_type=actor.kind,
                actor_id=actor.id,
                action="agent.updated",
                resource_type="agent",
                resource_id=agent_id,
                metadata={"fields": sorted(updates)},
            )
            result = await one(tx, "SELECT * FROM agents WHERE id=:id", {"id": agent_id})
        result["capabilities"] = json.loads(result["capabilities"] or "[]")
        result["adapter_config"] = (
            json.loads(result["adapter_config"]) if result["adapter_config"] else None
        )
        return result

    async def patch_user(
        self, user_id: str, fields: dict[str, Any], actor: Principal
    ) -> dict[str, Any]:
        require_admin(actor)
        allowed = {"display_name", "role", "is_active"}
        updates = {
            key: value for key, value in fields.items() if key in allowed and value is not None
        }
        async with self.db.write_tx() as tx:
            user = await one(tx, "SELECT id,email FROM users WHERE id=:id", {"id": user_id})
            if not user:
                raise not_found()
            if updates:
                params = {**updates, "id": user_id, "now": utc_now()}
                await tx.execute(
                    text(
                        "UPDATE users SET "
                        + ",".join(f"{key}=:{key}" for key in updates)
                        + ",updated_at=:now WHERE id=:id"
                    ),
                    params,
                )
                if updates.get("is_active") is False:
                    await tx.execute(
                        text(
                            "UPDATE sessions SET revoked_at=:now WHERE user_id=:id AND revoked_at IS NULL"
                        ),
                        {"now": utc_now(), "id": user_id},
                    )
            await insert_audit(
                tx,
                actor_type=actor.kind,
                actor_id=actor.id,
                action="user.updated",
                resource_type="user",
                resource_id=user_id,
                metadata={"fields": sorted(updates)},
            )
            result = await one(
                tx,
                "SELECT id,email,display_name,role,is_active,created_at,last_login_at FROM users WHERE id=:id",
                {"id": user_id},
            )
        result["is_active"] = bool(result["is_active"])
        return result

    async def patch_project(
        self, project_id: str, fields: dict[str, Any], actor: Principal
    ) -> dict[str, Any]:
        require_admin(actor)
        allowed = {"name", "description", "instructions", "is_active"}
        updates = {
            key: value
            for key, value in fields.items()
            if key in allowed and (value is not None or key in {"description", "instructions"})
        }
        async with self.db.write_tx() as tx:
            project = await one(
                tx, "SELECT id FROM projects WHERE id=:id OR slug=:id", {"id": project_id}
            )
            if not project:
                raise not_found()
            if updates:
                params = {**updates, "id": project["id"], "now": utc_now()}
                await tx.execute(
                    text(
                        "UPDATE projects SET "
                        + ",".join(f"{key}=:{key}" for key in updates)
                        + ",updated_at=:now WHERE id=:id"
                    ),
                    params,
                )
            await insert_audit(
                tx,
                actor_type=actor.kind,
                actor_id=actor.id,
                action="project.updated",
                resource_type="project",
                resource_id=project["id"],
                metadata={"fields": sorted(updates)},
            )
            return await one(tx, "SELECT * FROM projects WHERE id=:id", {"id": project["id"]})

    async def list_users(self, principal: Principal) -> list[dict[str, Any]]:
        require_admin(principal)
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,email,display_name,role,is_active,created_at,last_login_at FROM users ORDER BY email"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [{**dict(row), "is_active": bool(row["is_active"])} for row in rows]

    async def list_agents_admin(self, principal: Principal) -> list[dict[str, Any]]:
        require_admin(principal)
        async with self.db.engine.connect() as conn:
            rows = (
                (await conn.execute(text("SELECT * FROM agents ORDER BY name COLLATE NOCASE")))
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            data = dict(row)
            data["capabilities"] = json.loads(data["capabilities"] or "[]")
            data["adapter_config"] = (
                json.loads(data["adapter_config"]) if data["adapter_config"] else None
            )
            result.append(data)
        return result

    async def list_projects(self, principal: Principal) -> list[dict[str, Any]]:
        if not principal.is_user:
            raise fail("insufficient_role", "Dashboard access is required.", 403)
        async with self.db.engine.connect() as conn:
            if principal.is_admin:
                query = text("SELECT * FROM projects WHERE is_active=1 ORDER BY slug")
                rows = (await conn.execute(query)).mappings().all()
            else:
                query = text("""SELECT p.*,upa.role AS access_role FROM user_project_access upa
                               JOIN projects p ON p.id=upa.project_id
                               WHERE upa.user_id=:user AND p.is_active=1 ORDER BY p.slug""")
                rows = (await conn.execute(query, {"user": principal.id})).mappings().all()
        return [{**dict(row), "is_active": bool(row["is_active"])} for row in rows]

    async def list_agent_memberships(
        self, project_ref: str, principal: Principal
    ) -> list[dict[str, Any]]:
        require_admin(principal)
        async with self.db.engine.connect() as conn:
            project = await one(
                conn, "SELECT id FROM projects WHERE id=:ref OR slug=:ref", {"ref": project_ref}
            )
            if not project:
                raise not_found()
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        text("""SELECT a.id,a.name,a.agent_type,a.status,pa.role,pa.created_at FROM project_agents pa
                       JOIN agents a ON a.id=pa.agent_id WHERE pa.project_id=:project ORDER BY a.name"""),
                        {"project": project["id"]},
                    )
                )
                .mappings()
                .all()
            ]

    async def get_agent_membership(
        self, project_ref: str, agent_id: str, principal: Principal
    ) -> dict[str, Any]:
        require_admin(principal)
        async with self.db.engine.connect() as conn:
            project = await one(
                conn,
                "SELECT id,slug FROM projects WHERE id=:ref OR slug=:ref",
                {"ref": project_ref},
            )
            if not project:
                raise not_found()
            membership = await one(
                conn,
                "SELECT role,created_at FROM project_agents WHERE project_id=:project AND agent_id=:agent",
                {"project": project["id"], "agent": agent_id},
            )
        if membership is None:
            raise not_found()
        return {"project": project["slug"], "agent_id": agent_id, **membership}

    async def get_user_membership(
        self, project_ref: str, user_id: str, principal: Principal
    ) -> dict[str, Any]:
        require_admin(principal)
        async with self.db.engine.connect() as conn:
            project = await one(
                conn,
                "SELECT id,slug FROM projects WHERE id=:ref OR slug=:ref",
                {"ref": project_ref},
            )
            if not project:
                raise not_found()
            membership = await one(
                conn,
                "SELECT role,created_at FROM user_project_access WHERE project_id=:project AND user_id=:user",
                {"project": project["id"], "user": user_id},
            )
        if membership is None:
            raise not_found()
        return {"project": project["slug"], "user_id": user_id, **membership}

    async def list_audit(
        self, principal: Principal, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        require_admin(principal)
        limit = max(1, min(limit, 100))
        async with self.db.engine.connect() as conn:
            rows = await all_rows(
                conn,
                "SELECT * FROM audit_logs WHERE (:after IS NULL OR created_at<:after) ORDER BY created_at DESC,id DESC LIMIT :limit",
                {"after": after, "limit": limit},
            )
        return [
            {**row, "metadata": json.loads(row["metadata"]) if row["metadata"] else {}}
            for row in rows
        ]

    async def create_project(
        self,
        slug: str,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        actor: Principal | None = None,
    ) -> dict[str, Any]:
        if actor is not None:
            require_admin(actor)
        if not PROJECT_SLUG_RE.fullmatch(slug) or not name.strip() or len(name) > 200:
            raise fail("validation_error", "Project slug or name is invalid.", 422)
        project_id = new_id()
        now = utc_now()
        async with self.db.write_tx() as tx:
            if await one(tx, "SELECT id FROM projects WHERE slug=:slug", {"slug": slug}):
                raise fail("validation_error", "A project with this slug already exists.", 422)
            await tx.execute(
                text("""INSERT INTO projects(id,slug,name,description,instructions,created_at,updated_at)
                        VALUES(:id,:slug,:name,:description,:instructions,:now,:now)"""),
                {
                    "id": project_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "instructions": instructions,
                    "now": now,
                },
            )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="project.created",
                resource_type="project",
                resource_id=project_id,
            )
        return {
            "id": project_id,
            "slug": slug,
            "name": name,
            "description": description,
            "instructions": instructions,
            "is_active": True,
        }

    async def set_agent_membership(
        self, project_id: str, agent_id: str, role: str | None, actor: Principal | None = None
    ) -> dict[str, Any]:
        if actor is not None:
            require_admin(actor)
        if role not in {None, "reader", "contributor", "maintainer"}:
            raise fail("validation_error", "Invalid agent project role.", 422)
        now = utc_now()
        async with self.db.write_tx() as tx:
            project = await one(
                tx, "SELECT id,slug FROM projects WHERE id=:id OR slug=:id", {"id": project_id}
            )
            if not project:
                raise not_found()
            project_id = project["id"]
            if not await one(tx, "SELECT id FROM agents WHERE id=:id", {"id": agent_id}):
                raise not_found()
            if role is None:
                await tx.execute(
                    text(
                        "DELETE FROM project_agents WHERE project_id=:project AND agent_id=:agent"
                    ),
                    {"project": project_id, "agent": agent_id},
                )
            else:
                await tx.execute(
                    text("""INSERT INTO project_agents(project_id,agent_id,role,created_at) VALUES(:project,:agent,:role,:now)
                            ON CONFLICT(project_id,agent_id) DO UPDATE SET role=excluded.role"""),
                    {"project": project_id, "agent": agent_id, "role": role, "now": now},
                )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="project.agent_membership_changed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                metadata={"agent_id": agent_id, "role": role},
            )
        return {"project": project["slug"], "agent_id": agent_id, "role": role}

    async def set_user_membership(
        self, project_id: str, user_id: str, role: str | None, actor: Principal | None = None
    ) -> dict[str, Any]:
        if actor is not None:
            require_admin(actor)
        if role not in {None, "viewer", "maintainer"}:
            raise fail("validation_error", "Invalid dashboard user project role.", 422)
        async with self.db.write_tx() as tx:
            project = await one(
                tx, "SELECT id,slug FROM projects WHERE id=:id OR slug=:id", {"id": project_id}
            )
            if not project:
                raise not_found()
            project_id = project["id"]
            if not await one(tx, "SELECT id FROM users WHERE id=:id", {"id": user_id}):
                raise not_found()
            if role is None:
                await tx.execute(
                    text(
                        "DELETE FROM user_project_access WHERE project_id=:project AND user_id=:user"
                    ),
                    {"project": project_id, "user": user_id},
                )
            else:
                await tx.execute(
                    text("""INSERT INTO user_project_access(user_id,project_id,role,created_at)
                            VALUES(:user,:project,:role,:now)
                            ON CONFLICT(user_id,project_id) DO UPDATE SET role=excluded.role"""),
                    {"user": user_id, "project": project_id, "role": role, "now": utc_now()},
                )
            await insert_audit(
                tx,
                actor_type=actor.kind if actor else "system",
                actor_id=actor.id if actor else "system",
                action="project.user_membership_changed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                metadata={"user_id": user_id, "role": role},
            )
        return {"project": project["slug"], "user_id": user_id, "role": role}


auth_service = AuthService()
