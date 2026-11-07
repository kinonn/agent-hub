"""GitHub webhook authentication, validation, and durable deduplication."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from sqlalchemy import text

from src.config import Settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.database.types import utc_now
from src.errors import fail


class GitHubWebhookService:
    def __init__(self, database: Database | None = None):
        self.db = database or default_db

    async def accept(
        self,
        settings: Settings,
        *,
        signature: str | None,
        delivery_id: str | None,
        event_name: str | None,
        body: bytes,
        database: Database | None = None,
    ) -> dict[str, str]:
        if not settings.github_webhooks_enabled:
            raise fail("not_found", "Webhook endpoint is disabled.", 404)
        if not signature or not delivery_id or not event_name:
            raise fail("bad_request", "Missing GitHub webhook headers.", 400)

        secret_path = Path(settings.github_webhook_secret_file)
        if not secret_path.is_file():
            raise fail("unavailable", "Webhook secret is not configured.", 503)
        secret = secret_path.read_bytes().strip()
        if not secret:
            raise fail("unavailable", "Webhook secret is not configured.", 503)
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise fail("unauthenticated", "Invalid GitHub webhook signature.", 401)

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise fail("bad_request", "Webhook payload must be a JSON object.", 400) from exc
        if not isinstance(payload, dict):
            raise fail("bad_request", "Webhook payload must be a JSON object.", 400)

        action = payload.get("action")
        pull_number = None
        status = "ignored"
        if event_name == "pull_request":
            repository = payload.get("repository")
            pull_request = payload.get("pull_request")
            if not isinstance(repository, dict) or not isinstance(pull_request, dict):
                raise fail("bad_request", "Pull request webhook payload is incomplete.", 400)
            full_name = repository.get("full_name")
            expected_repo = (
                f"{settings.github_repository_owner}/{settings.github_knowledge_repository}"
            )
            if not isinstance(full_name, str) or full_name.lower() != expected_repo.lower():
                raise fail("forbidden", "Webhook repository is not configured.", 403)
            number = pull_request.get("number")
            if (
                action in {"opened", "reopened", "closed", "synchronize"}
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
            ):
                pull_number = number
                status = "pending"
        elif event_name != "ping":
            return {"status": "ignored"}

        now = utc_now()
        db = database or self.db
        async with db.write_tx() as tx:
            existing = await tx.execute(
                text("SELECT delivery_id FROM github_deliveries WHERE delivery_id=:id"),
                {"id": delivery_id},
            )
            if existing.first():
                return {"status": "duplicate"}
            await tx.execute(
                text("""INSERT INTO github_deliveries(delivery_id,event_name,action,pull_request_number,received_at,
                           processed_at,processing_status,error_detail)
                        VALUES(:id,:event,:action,:number,:now,:processed,:status,NULL)"""),
                {
                    "id": delivery_id,
                    "event": event_name,
                    "action": action,
                    "number": pull_number,
                    "now": now,
                    "processed": now if status == "ignored" else None,
                    "status": status,
                },
            )
        return {"status": "accepted" if status == "pending" else "ignored"}


github_webhook_service = GitHubWebhookService()
