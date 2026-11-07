"""Artifact staging, GitHub publication/reconciliation, and FTS5 search."""

from __future__ import annotations

import hmac
import random
import time
from typing import Any

import httpx
from sqlalchemy import text

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_after, utc_now
from src.errors import fail, forbidden, not_found
from src.security.content_policy import validate_artifact
from src.security.tokens import Principal, sha256_hex
from src.services.common import (
    all_rows,
    artifact_dict,
    decode_cursor,
    emit_event,
    encode_cursor,
    fetch_artifact,
    fingerprint,
    insert_audit,
    new_id,
    one,
    require_agent_project,
    require_project_access,
    resolve_project,
)
from src.services.github_service import GitHubClient, GitHubError, strip_front_matter

TYPE_DIRECTORY = {
    "research": "research",
    "specification": "specifications",
    "decision": "decisions",
    "implementation": "implementation",
    "review": "reviews",
}


class ArtifactService:
    def __init__(
        self,
        database: Database | None = None,
        settings: Settings | None = None,
        github: GitHubClient | None = None,
    ):
        self.db = database or default_db
        self.settings = settings or get_settings()
        self.github = github or GitHubClient(self.settings)
        self._reconcile_etags: dict[int, str] = {}
        self._reconcile_retry_at: dict[int, float] = {}
        self._reconcile_failures: dict[int, int] = {}

    async def publish_artifact(
        self,
        principal: Principal,
        project_ref: str,
        artifact_type: str,
        title: str,
        content: str,
        *,
        summary: str | None = None,
        document_key: str | None = None,
        task_id: str | None = None,
        lease_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not principal.is_agent:
            raise forbidden("insufficient_role")
        if artifact_type not in TYPE_DIRECTORY:
            raise fail("validation_error", "Artifact type is invalid.", 422)
        now, artifact_id = utc_now(), new_id()
        async with self.db.write_tx() as tx:
            project = await resolve_project(tx, project_ref)
            await require_agent_project(tx, project["id"], principal, ("contributor", "maintainer"))
            try:
                request_fp = fingerprint(
                    {
                        "agent_id": principal.id,
                        "project_id": project["id"],
                        "artifact_type": artifact_type,
                        "title": title,
                        "content": content,
                        "summary": summary,
                        "document_key": document_key,
                        "task_id": task_id,
                    }
                )
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise fail(
                    "validation_error", "Artifact request contains invalid text.", 422
                ) from exc
            # Idempotency precedes open-version checks, so response loss is safe.
            if idempotency_key:
                existing = await one(
                    tx,
                    """SELECT id,request_fingerprint FROM artifacts
                       WHERE project_id=:project AND idempotency_key=:key""",
                    {"project": project["id"], "key": idempotency_key},
                )
                if existing:
                    if existing["request_fingerprint"] != request_fp:
                        raise fail(
                            "idempotency_conflict",
                            "Idempotency key was used for a different artifact.",
                            409,
                        )
                    row = await fetch_artifact(tx, existing["id"])
                    return {"artifact": artifact_dict(row), "idempotent_replay": True}
            if task_id:
                task = await one(
                    tx,
                    "SELECT * FROM tasks WHERE id=:id AND project_id=:project",
                    {"id": task_id, "project": project["id"]},
                )
                if task is None:
                    raise not_found()
                if task["status"] not in {"claimed", "in_progress"}:
                    raise fail(
                        "task_not_leased", "The task does not currently have an active lease.", 409
                    )
                supplied_hash = sha256_hex(lease_token or "")
                if (
                    task["lease_owner_agent_id"] != principal.id
                    or not task["lease_token_hash"]
                    or not hmac.compare_digest(task["lease_token_hash"], supplied_hash)
                ):
                    raise forbidden("not_lease_owner")
            if not self.settings.github_repository_owner:
                raise fail("unavailable", "The GitHub knowledge repository is not configured.", 503)
            key = validate_artifact(title, content, document_key, self.settings.max_artifact_bytes)
            content_hash = sha256_hex(content)
            opened = await one(
                tx,
                """SELECT id FROM artifacts WHERE project_id=:project AND artifact_type=:type
                   AND document_key=:key AND publication_status IN ('draft','in_review') LIMIT 1""",
                {"project": project["id"], "type": artifact_type, "key": key},
            )
            if opened:
                raise fail(
                    "artifact_open_version_exists",
                    "An open version of this document already exists.",
                    409,
                )
            latest = await one(
                tx,
                """SELECT id,version FROM artifacts WHERE project_id=:project AND artifact_type=:type
                   AND document_key=:key ORDER BY version DESC LIMIT 1""",
                {"project": project["id"], "type": artifact_type, "key": key},
            )
            version = (latest["version"] + 1) if latest else 1
            supersedes = await one(
                tx,
                """SELECT id FROM artifacts WHERE project_id=:project AND artifact_type=:type
                   AND document_key=:key AND publication_status='published' ORDER BY version DESC LIMIT 1""",
                {"project": project["id"], "type": artifact_type, "key": key},
            )
            file_path = f"projects/{project['slug']}/{TYPE_DIRECTORY[artifact_type]}/{key}.md"
            branch = f"agenthub/{project['slug']}/{artifact_id}"
            await tx.execute(
                text("""INSERT INTO artifacts(id,project_id,task_id,created_by_agent_id,artifact_type,title,summary,
                           document_key,version,supersedes_artifact_id,content,content_sha256,repository_owner,
                           repository_name,branch_name,file_path,publication_status,idempotency_key,
                           request_fingerprint,created_at,updated_at)
                        VALUES(:id,:project,:task,:agent,:type,:title,:summary,:key,:version,:supersedes,:content,
                           :hash,:owner,:repo,:branch,:path,'draft',:idem,:fingerprint,:now,:now)"""),
                {
                    "id": artifact_id,
                    "project": project["id"],
                    "task": task_id,
                    "agent": principal.id,
                    "type": artifact_type,
                    "title": title,
                    "summary": summary,
                    "key": key,
                    "version": version,
                    "supersedes": supersedes["id"] if supersedes else None,
                    "content": content,
                    "hash": content_hash,
                    "owner": self.settings.github_repository_owner,
                    "repo": self.settings.github_knowledge_repository,
                    "branch": branch,
                    "path": file_path,
                    "idem": idempotency_key,
                    "fingerprint": request_fp,
                    "now": now,
                },
            )
            await emit_event(
                tx,
                "artifact.created",
                "artifact",
                artifact_id,
                project["id"],
                {"artifact_id": artifact_id, "status": "draft"},
                now=now,
            )
            await insert_audit(
                tx,
                actor_type="agent",
                actor_id=principal.id,
                action="artifact.staged",
                resource_type="artifact",
                resource_id=artifact_id,
                project_id=project["id"],
                metadata={"artifact_type": artifact_type, "version": version},
            )
            row = await fetch_artifact(tx, artifact_id)
        return {"artifact": artifact_dict(row, include_error=True), "idempotent_replay": False}

    async def get_artifact(
        self, principal: Principal, artifact_id: str, include_content: bool = False
    ) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = await fetch_artifact(conn, artifact_id)
            role = await require_project_access(conn, row["project_id"], principal)
            if row["publication_status"] != "published":
                if (
                    principal.is_agent
                    and role == "contributor"
                    and row["created_by_agent_id"] != principal.id
                ):
                    raise not_found()
                if role == "reader" or (principal.is_user and role != "maintainer"):
                    raise not_found()
            result = artifact_dict(row, include_content=include_content, include_error=True)
        return {"artifact": result}

    async def list_project_artifacts(
        self,
        principal: Principal,
        project_ref: str,
        *,
        status: str | None = None,
        artifact_type: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters = {"project": project_ref, "status": status, "artifact_type": artifact_type}
        after = decode_cursor(self.settings.app_secret, cursor, filters) if cursor else None
        async with self.db.engine.connect() as conn:
            project = await resolve_project(conn, project_ref)
            role = await require_project_access(conn, project["id"], principal)
            where = ["a.project_id=:project"]
            params: dict[str, Any] = {"project": project["id"], "limit": limit + 1}
            if status:
                where.append("a.publication_status=:status")
                params["status"] = status
            if artifact_type:
                where.append("a.artifact_type=:type")
                params["type"] = artifact_type
            if role == "reader" or (principal.is_user and role != "maintainer"):
                where.append("a.publication_status='published'")
            elif principal.is_agent and role == "contributor":
                where.append("(a.publication_status='published' OR a.created_by_agent_id=:agent)")
                params["agent"] = principal.id
            if after:
                where.append("(a.created_at<:created OR (a.created_at=:created AND a.id<:id))")
                params.update({"created": after[0], "id": after[1]})
            rows = await all_rows(
                conn,
                f"""SELECT a.*,p.slug AS project_slug FROM artifacts a JOIN projects p ON p.id=a.project_id
                   WHERE {" AND ".join(where)} ORDER BY a.created_at DESC,a.id DESC LIMIT :limit""",
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
        return {"artifacts": [artifact_dict(row) for row in page], "next_cursor": next_cursor}

    async def search_knowledge(
        self,
        principal: Principal,
        project_ref: str,
        query: str,
        *,
        artifact_type: str | None = None,
        include_unpublished: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        terms = query.split()
        if not terms:
            raise fail(
                "validation_error", "Search query must contain at least one searchable term.", 422
            )
        # Every whitespace-delimited token is a quoted phrase, so FTS operators, quotes,
        # column filters, and wildcards are always treated as literal user text.
        match_query = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        async with self.db.engine.connect() as conn:
            project = await resolve_project(conn, project_ref)
            role = await require_project_access(conn, project["id"], principal)
            params: dict[str, Any] = {
                "project": project["id"],
                "query": match_query,
                "limit": limit,
            }
            type_sql = ""
            if artifact_type:
                type_sql = " AND a.artifact_type=:type"
                params["type"] = artifact_type
            rows = await all_rows(
                conn,
                f"""SELECT a.*,p.slug AS project_slug,bm25(artifacts_fts) AS score,
                          snippet(artifacts_fts,4,'<mark>','</mark>','…',18) AS snippet
                   FROM artifacts_fts JOIN artifacts a ON a.id=artifacts_fts.artifact_id
                   JOIN projects p ON p.id=a.project_id
                   WHERE artifacts_fts MATCH :query AND a.project_id=:project
                     AND a.publication_status='published' {type_sql}
                   ORDER BY bm25(artifacts_fts) LIMIT :limit""",
                params,
            )
            results = [self._search_result(row, row["snippet"], row["score"]) for row in rows]
            can_unpublished = include_unpublished and (
                role == "maintainer" or (principal.is_agent and role == "contributor")
            )
            if can_unpublished and len(results) < limit:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                unpublished_where = [
                    "a.project_id=:project",
                    "a.publication_status IN ('draft','in_review','rejected','failed')",
                    "(a.title LIKE :like ESCAPE '\\' OR COALESCE(a.summary,'') LIKE :like ESCAPE '\\')",
                ]
                extra = {
                    "project": project["id"],
                    "like": f"%{escaped}%",
                    "limit": limit - len(results),
                }
                if artifact_type:
                    unpublished_where.append("a.artifact_type=:type")
                    extra["type"] = artifact_type
                if principal.is_agent and role != "maintainer":
                    unpublished_where.append("a.created_by_agent_id=:agent")
                    extra["agent"] = principal.id
                unpublished = await all_rows(
                    conn,
                    f"""SELECT a.*,p.slug AS project_slug,a.summary AS snippet,0.0 AS score
                       FROM artifacts a JOIN projects p ON p.id=a.project_id WHERE {" AND ".join(unpublished_where)}
                       ORDER BY a.created_at DESC,a.id DESC LIMIT :limit""",
                    extra,
                )
                results.extend(
                    self._search_result(row, row["snippet"] or row["title"], 0.0)
                    for row in unpublished
                )
        return {"results": results[:limit]}

    async def publish_due(self, batch_size: int = 5) -> int:
        if not self.github.configured:
            return 0
        now = utc_now()
        async with self.db.write_tx() as tx:
            due = await all_rows(
                tx,
                """SELECT a.*,p.slug AS project_slug,ag.name AS agent_name FROM artifacts a
                   JOIN projects p ON p.id=a.project_id LEFT JOIN agents ag ON ag.id=a.created_by_agent_id
                   WHERE a.publication_status='draft' AND (a.next_publish_attempt_at IS NULL OR a.next_publish_attempt_at<=:now)
                   ORDER BY a.created_at LIMIT :limit""",
                {"now": now, "limit": batch_size},
            )
            for row in due:
                delay = min(300, 30 * (2 ** min(row["publish_attempts"], 4)))
                await tx.execute(
                    text(
                        "UPDATE artifacts SET publish_attempts=publish_attempts+1,next_publish_attempt_at=:next,updated_at=:now WHERE id=:id"
                    ),
                    {"next": utc_after(delay, now), "now": now, "id": row["id"]},
                )
        for artifact in due:
            try:
                pushed = await self.github.prepare_pull_request(
                    artifact, artifact.get("agent_name") or "unknown agent"
                )
            except GitHubError as exc:
                await self._record_publish_error(artifact, exc)
                continue
            except Exception as exc:
                await self._record_publish_error(artifact, GitHubError(500, type(exc).__name__))
                continue
            async with self.db.write_tx() as tx:
                current = await one(
                    tx, "SELECT * FROM artifacts WHERE id=:id", {"id": artifact["id"]}
                )
                if not current or current["publication_status"] != "draft":
                    continue
                now = utc_now()
                await tx.execute(
                    text("""UPDATE artifacts SET publication_status='in_review',branch_commit_sha=:sha,
                           pull_request_number=:number,pull_request_url=:url,next_publish_attempt_at=NULL,
                           last_error=NULL,updated_at=:now WHERE id=:id"""),
                    {
                        "sha": pushed["branch_commit_sha"],
                        "number": pushed["pull_request_number"],
                        "url": pushed["pull_request_url"],
                        "now": now,
                        "id": artifact["id"],
                    },
                )
                await emit_event(
                    tx,
                    "artifact.in_review",
                    "artifact",
                    artifact["id"],
                    artifact["project_id"],
                    {"artifact_id": artifact["id"], "status": "in_review"},
                    now=now,
                )
        return len(due)

    async def _record_publish_error(self, artifact: dict[str, Any], error: GitHubError) -> None:
        now = utc_now()
        terminal = (
            not error.retryable
            or artifact["publish_attempts"] + 1 >= self.settings.publish_max_attempts
        )
        jitter = random.randint(0, 10)
        retry_after = min(
            error.retry_after
            or min(600, 30 * (2 ** min(artifact["publish_attempts"], 4)) + jitter),
            86400,
        )
        async with self.db.write_tx() as tx:
            current = await one(
                tx, "SELECT publication_status FROM artifacts WHERE id=:id", {"id": artifact["id"]}
            )
            if not current or current["publication_status"] != "draft":
                return
            status = "failed" if terminal else "draft"
            await tx.execute(
                text(
                    "UPDATE artifacts SET publication_status=:status,last_error=:error,next_publish_attempt_at=:next,updated_at=:now WHERE id=:id"
                ),
                {
                    "status": status,
                    "error": str(error)[:400],
                    "next": None if terminal else utc_after(retry_after, now),
                    "now": now,
                    "id": artifact["id"],
                },
            )
            if terminal:
                await emit_event(
                    tx,
                    "artifact.failed",
                    "artifact",
                    artifact["id"],
                    artifact["project_id"],
                    {"artifact_id": artifact["id"], "status": "failed"},
                    now=now,
                )

    async def reconcile_open(self, batch_size: int = 50) -> int:
        async with self.db.engine.connect() as conn:
            artifacts = await all_rows(
                conn,
                """SELECT * FROM artifacts WHERE publication_status IN ('in_review','rejected')
                   AND pull_request_number IS NOT NULL ORDER BY updated_at LIMIT :limit""",
                {"limit": batch_size},
            )
        reconciled = 0
        for artifact in artifacts:
            pull_number = int(artifact["pull_request_number"])
            if self._reconcile_retry_at.get(pull_number, 0) > time.monotonic():
                continue
            try:
                changed = await self._reconcile_artifact(artifact)
            except (GitHubError, httpx.HTTPError) as error:
                self._back_off_reconciliation(pull_number, error)
                continue
            self._reconcile_failures.pop(pull_number, None)
            self._reconcile_retry_at.pop(pull_number, None)
            if changed:
                reconciled += 1
        return reconciled

    async def reconcile_one(self, pull_request_number: int) -> None:
        async with self.db.engine.connect() as conn:
            artifacts = await all_rows(
                conn,
                "SELECT * FROM artifacts WHERE pull_request_number=:number",
                {"number": pull_request_number},
            )
        for artifact in artifacts:
            try:
                await self._reconcile_artifact(artifact)
            except (GitHubError, httpx.HTTPError) as error:
                self._back_off_reconciliation(pull_request_number, error)
                raise

    async def _reconcile_artifact(self, artifact: dict[str, Any]) -> bool:
        pull_number = int(artifact["pull_request_number"])
        pull, etag, not_modified = await self.github.get_pull_request_conditional(
            artifact, self._reconcile_etags.get(pull_number)
        )
        if not_modified or pull is None:
            if not_modified:
                self._remember_reconcile_etag(pull_number, etag)
            return False
        if pull.get("merged_at"):
            merge_sha = pull.get("merge_commit_sha")
            if not merge_sha:
                return False
            remote_content = strip_front_matter(await self.github.fetch_file(artifact, merge_sha))
            await self._mark_published(artifact, remote_content, merge_sha)
            self._remember_reconcile_etag(pull_number, etag)
            return True
        if pull.get("state") == "closed":
            await self._mark_rejected(artifact)
            self._remember_reconcile_etag(pull_number, etag)
            return True
        if artifact["publication_status"] == "rejected" and pull.get("state") == "open":
            await self._mark_in_review(artifact)
            self._remember_reconcile_etag(pull_number, etag)
            return True
        self._remember_reconcile_etag(pull_number, etag)
        return False

    def _remember_reconcile_etag(self, pull_number: int, etag: str | None) -> None:
        if etag:
            self._reconcile_etags[pull_number] = etag
        else:
            self._reconcile_etags.pop(pull_number, None)

    def _back_off_reconciliation(self, pull_number: int, error: Exception) -> None:
        failures = self._reconcile_failures.get(pull_number, 0) + 1
        self._reconcile_failures[pull_number] = failures
        delay = min(30 * (2 ** min(failures - 1, 10)), 3600)
        if isinstance(error, GitHubError) and error.retry_after is not None:
            delay = max(delay, error.retry_after)
        self._reconcile_retry_at[pull_number] = time.monotonic() + min(delay, 86400)

    async def _mark_published(self, artifact: dict[str, Any], content: str, merge_sha: str) -> None:
        now = utc_now()
        actual_hash = sha256_hex(content)
        async with self.db.write_tx() as tx:
            current = await one(tx, "SELECT * FROM artifacts WHERE id=:id", {"id": artifact["id"]})
            if not current or current["publication_status"] not in {"in_review", "rejected"}:
                return
            if actual_hash != current["content_sha256"]:
                await insert_audit(
                    tx,
                    actor_type="system",
                    actor_id="system",
                    action="artifact.reviewer_edit_accepted",
                    resource_type="artifact",
                    resource_id=artifact["id"],
                    project_id=artifact["project_id"],
                    metadata={
                        "expected_sha256": current["content_sha256"],
                        "actual_sha256": actual_hash,
                    },
                )
            if current["supersedes_artifact_id"]:
                await tx.execute(
                    text("DELETE FROM artifacts_fts WHERE artifact_id=:id"),
                    {"id": current["supersedes_artifact_id"]},
                )
                await tx.execute(
                    text("""UPDATE artifacts SET publication_status='superseded',updated_at=:now
                           WHERE id=:id AND publication_status='published'"""),
                    {"now": now, "id": current["supersedes_artifact_id"]},
                )
                await emit_event(
                    tx,
                    "artifact.superseded",
                    "artifact",
                    current["supersedes_artifact_id"],
                    current["project_id"],
                    {"artifact_id": current["supersedes_artifact_id"], "status": "superseded"},
                    now=now,
                )
            await tx.execute(
                text("""UPDATE artifacts SET publication_status='published',content=:content,content_sha256=:hash,
                       commit_sha=:sha,published_at=:now,updated_at=:now,last_error=NULL,next_publish_attempt_at=NULL
                       WHERE id=:id"""),
                {
                    "content": content,
                    "hash": actual_hash,
                    "sha": merge_sha,
                    "now": now,
                    "id": artifact["id"],
                },
            )
            await tx.execute(
                text("DELETE FROM artifacts_fts WHERE artifact_id=:id"), {"id": artifact["id"]}
            )
            await tx.execute(
                text("""INSERT INTO artifacts_fts(artifact_id,project_id,title,summary,content)
                       SELECT id,project_id,title,COALESCE(summary,''),content FROM artifacts WHERE id=:id"""),
                {"id": artifact["id"]},
            )
            await emit_event(
                tx,
                "artifact.published",
                "artifact",
                artifact["id"],
                artifact["project_id"],
                {"artifact_id": artifact["id"], "status": "published"},
                now=now,
            )
            await insert_audit(
                tx,
                actor_type="system",
                actor_id="system",
                action="artifact.published",
                resource_type="artifact",
                resource_id=artifact["id"],
                project_id=artifact["project_id"],
                metadata={"commit_sha": merge_sha, "version": current["version"]},
            )

    async def _mark_rejected(self, artifact: dict[str, Any]) -> None:
        now = utc_now()
        async with self.db.write_tx() as tx:
            current = await one(
                tx, "SELECT publication_status FROM artifacts WHERE id=:id", {"id": artifact["id"]}
            )
            if not current or current["publication_status"] not in {"in_review", "rejected"}:
                return
            if current["publication_status"] == "rejected":
                return
            await tx.execute(
                text(
                    "UPDATE artifacts SET publication_status='rejected',updated_at=:now WHERE id=:id"
                ),
                {"now": now, "id": artifact["id"]},
            )
            await tx.execute(
                text("DELETE FROM artifacts_fts WHERE artifact_id=:id"), {"id": artifact["id"]}
            )
            await emit_event(
                tx,
                "artifact.rejected",
                "artifact",
                artifact["id"],
                artifact["project_id"],
                {"artifact_id": artifact["id"], "status": "rejected"},
                now=now,
            )

    async def _mark_in_review(self, artifact: dict[str, Any]) -> None:
        now = utc_now()
        async with self.db.write_tx() as tx:
            result = await tx.execute(
                text(
                    "UPDATE artifacts SET publication_status='in_review',updated_at=:now WHERE id=:id AND publication_status='rejected'"
                ),
                {"now": now, "id": artifact["id"]},
            )
            if result.rowcount != 1:
                return
            await emit_event(
                tx,
                "artifact.in_review",
                "artifact",
                artifact["id"],
                artifact["project_id"],
                {"artifact_id": artifact["id"], "status": "in_review"},
                now=now,
            )

    async def reindex(self) -> int:
        async with self.db.write_tx() as tx:
            await tx.execute(text("DELETE FROM artifacts_fts"))
            await tx.execute(
                text("""INSERT INTO artifacts_fts(artifact_id,project_id,title,summary,content)
                       SELECT a.id,a.project_id,a.title,COALESCE(a.summary,''),a.content FROM artifacts a
                       WHERE a.publication_status='published' AND NOT EXISTS (
                         SELECT 1 FROM artifacts newer WHERE newer.project_id=a.project_id
                           AND newer.artifact_type=a.artifact_type AND newer.document_key=a.document_key
                           AND newer.publication_status='published' AND newer.version>a.version)""")
            )
            count = (await tx.execute(text("SELECT count(*) FROM artifacts_fts"))).scalar_one()
        return int(count)

    async def refresh_published(self) -> int:
        if not self.github.configured:
            raise fail("unavailable", "GitHub credentials are not configured.", 503)
        async with self.db.engine.connect() as conn:
            rows = await all_rows(
                conn, "SELECT * FROM artifacts WHERE publication_status='published'"
            )
        refreshed = 0
        branch_heads: dict[tuple[str, str], str] = {}
        for artifact in rows:
            try:
                repository = (artifact["repository_owner"], artifact["repository_name"])
                if repository not in branch_heads:
                    branch_heads[repository] = await self.github.get_branch_head(
                        *repository, self.settings.github_default_branch
                    )
                head_sha = branch_heads[repository]
                remote = strip_front_matter(await self.github.fetch_file(artifact, head_sha))
            except GitHubError:
                continue
            remote_hash = sha256_hex(remote)
            if remote_hash == artifact["content_sha256"]:
                continue
            async with self.db.write_tx() as tx:
                current = await one(
                    tx,
                    "SELECT * FROM artifacts WHERE id=:id AND publication_status='published'",
                    {"id": artifact["id"]},
                )
                if not current:
                    continue
                now = utc_now()
                await tx.execute(
                    text(
                        "UPDATE artifacts SET content=:content,content_sha256=:hash,commit_sha=:sha,updated_at=:now WHERE id=:id"
                    ),
                    {
                        "content": remote,
                        "hash": remote_hash,
                        "sha": head_sha,
                        "now": now,
                        "id": artifact["id"],
                    },
                )
                await tx.execute(
                    text("DELETE FROM artifacts_fts WHERE artifact_id=:id"), {"id": artifact["id"]}
                )
                await tx.execute(
                    text("""INSERT INTO artifacts_fts(artifact_id,project_id,title,summary,content)
                           VALUES(:id,:project,:title,:summary,:content)"""),
                    {
                        "id": artifact["id"],
                        "project": artifact["project_id"],
                        "title": artifact["title"],
                        "summary": artifact["summary"] or "",
                        "content": remote,
                    },
                )
                await insert_audit(
                    tx,
                    actor_type="system",
                    actor_id="system",
                    action="artifact.default_branch_refresh",
                    resource_type="artifact",
                    resource_id=artifact["id"],
                    project_id=artifact["project_id"],
                    metadata={
                        "old_sha256": artifact["content_sha256"],
                        "new_sha256": remote_hash,
                        "commit_sha": head_sha,
                    },
                )
            refreshed += 1
        return refreshed

    @staticmethod
    def _search_result(row: dict[str, Any], snippet: str, score: float) -> dict[str, Any]:
        return {
            "artifact_id": row["id"],
            "project": row["project_slug"],
            "artifact_type": row["artifact_type"],
            "title": row["title"],
            "snippet": snippet or row["title"],
            "version": row["version"],
            "publication_status": row["publication_status"],
            "repository": f"{row['repository_owner']}/{row['repository_name']}",
            "file_path": row["file_path"],
            "commit_sha": row["commit_sha"],
            "pull_request_url": row["pull_request_url"],
            "score": score,
        }


artifact_service = ArtifactService()
