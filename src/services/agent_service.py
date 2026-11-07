from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_now
from src.errors import forbidden, not_found
from src.security.tokens import Principal
from src.services.common import (
    all_rows,
    one,
    require_project_access,
    resolve_project,
)


class AgentService:
    def __init__(self, database: Database | None = None):
        self.db = database or default_db

    async def register_agent(
        self, principal: Principal, description: str | None, capabilities: list[str]
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        async with self.db.write_tx() as tx:
            await tx.execute(
                text(
                    "UPDATE agents SET description=:description,capabilities=:caps,updated_at=:now WHERE id=:id"
                ),
                {
                    "description": description,
                    "caps": json.dumps(capabilities),
                    "now": utc_now(),
                    "id": principal.id,
                },
            )
            agent = await one(tx, "SELECT * FROM agents WHERE id=:id", {"id": principal.id})
            memberships = await all_rows(
                tx,
                """SELECT p.slug AS project,pa.role FROM project_agents pa JOIN projects p ON p.id=pa.project_id
                   WHERE pa.agent_id=:id AND p.is_active=1 ORDER BY p.slug""",
                {"id": principal.id},
            )
        if agent is None:
            raise not_found()
        return {"agent": self._profile(agent), "memberships": memberships}

    async def get_agent_status(
        self, principal: Principal, agent_id: str | None = None
    ) -> dict[str, Any]:
        target_id = agent_id or principal.id
        async with self.db.engine.connect() as conn:
            agent = await one(conn, "SELECT * FROM agents WHERE id=:id", {"id": target_id})
            if agent is None:
                raise not_found()
            if target_id != principal.id:
                if principal.is_admin:
                    shared = {"yes": 1}
                elif principal.is_agent:
                    shared = await one(
                        conn,
                        """SELECT 1 AS yes FROM project_agents caller
                           JOIN project_agents target ON target.project_id=caller.project_id
                           JOIN projects p ON p.id=caller.project_id
                           WHERE caller.agent_id=:caller AND target.agent_id=:target AND p.is_active=1 LIMIT 1""",
                        {"caller": principal.id, "target": target_id},
                    )
                else:
                    shared = await one(
                        conn,
                        """SELECT 1 AS yes FROM user_project_access caller
                           JOIN project_agents target ON target.project_id=caller.project_id
                           JOIN projects p ON p.id=caller.project_id
                           WHERE caller.user_id=:caller AND target.agent_id=:target AND p.is_active=1 LIMIT 1""",
                        {"caller": principal.id, "target": target_id},
                    )
                if shared is None:
                    raise not_found()
            if principal.is_admin:
                leases = await all_rows(
                    conn,
                    """SELECT t.id AS task_id,p.slug AS project,t.lease_expires_at FROM tasks t
                       JOIN projects p ON p.id=t.project_id WHERE p.is_active=1 AND t.lease_owner_agent_id=:agent
                       AND t.status IN ('claimed','in_progress') ORDER BY t.lease_expires_at""",
                    {"agent": target_id},
                )
            elif principal.is_agent:
                leases = await all_rows(
                    conn,
                    """SELECT t.id AS task_id,p.slug AS project,t.lease_expires_at FROM tasks t
                       JOIN projects p ON p.id=t.project_id
                       JOIN project_agents pa ON pa.project_id=t.project_id AND pa.agent_id=:caller
                       WHERE p.is_active=1 AND t.lease_owner_agent_id=:agent AND t.status IN ('claimed','in_progress')
                       ORDER BY t.lease_expires_at""",
                    {"agent": target_id, "caller": principal.id},
                )
            else:
                leases = await all_rows(
                    conn,
                    """SELECT t.id AS task_id,p.slug AS project,t.lease_expires_at FROM tasks t
                       JOIN projects p ON p.id=t.project_id
                       JOIN user_project_access upa ON upa.project_id=t.project_id AND upa.user_id=:caller
                       WHERE p.is_active=1 AND t.lease_owner_agent_id=:agent AND t.status IN ('claimed','in_progress')
                       ORDER BY t.lease_expires_at""",
                    {"agent": target_id, "caller": principal.id},
                )
        return {"agent": self._profile(agent), "active_leases": leases}

    async def list_agents(self, principal: Principal, project_ref: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            project = await resolve_project(conn, project_ref)
            await require_project_access(conn, project["id"], principal)
            agents = await all_rows(
                conn,
                """SELECT a.*,pa.role FROM project_agents pa JOIN agents a ON a.id=pa.agent_id
                   WHERE pa.project_id=:project AND a.status='active' ORDER BY a.name COLLATE NOCASE LIMIT 200""",
                {"project": project["id"]},
            )
        return {"agents": [self._profile(agent) | {"role": agent["role"]} for agent in agents]}

    @staticmethod
    def _profile(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_type": row["agent_type"],
            "description": row["description"],
            "status": row["status"],
            "capabilities": json.loads(row["capabilities"] or "[]"),
            "last_seen_at": row["last_seen_at"],
        }


agent_service = AgentService()
