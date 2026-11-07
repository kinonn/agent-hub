"""Typed configuration for the API and worker processes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = "production"
    log_level: str = "info"
    database_url: str = "sqlite+aiosqlite:////app/data/agenthub.db"
    allow_unsafe_db_fs: bool = False
    app_secret_key_file: str = "/run/secrets/app_secret_key"
    github_private_key_file: str = "/run/secrets/github_app_private_key"
    github_token_file: str = ""
    github_webhook_secret_file: str = "/run/secrets/github_webhook_secret"
    github_app_id: str = ""
    github_installation_id: str = ""
    github_repository_owner: str = ""
    github_knowledge_repository: str = "agenthub-knowledge"
    github_default_branch: str = "main"
    github_pr_poll_interval_seconds: int = 300
    github_webhooks_enabled: bool = False
    publish_max_attempts: int = 5
    task_lease_seconds: int = 120
    task_heartbeat_seconds: int = 40
    task_max_attempts: int = 3
    task_retry_backoff_seconds: int = 30
    task_retry_backoff_max_seconds: int = 600
    task_max_runtime_seconds: int = 14400
    worker_poll_interval_seconds: int = 10
    outbox_poll_interval_seconds: int = 5
    outbox_processing_timeout_seconds: int = 120
    outbox_max_attempts: int = 8
    sse_max_connections: int = 20
    max_request_bytes: int = 1_048_576
    max_task_payload_bytes: int = 65_536
    max_message_bytes: int = 65_536
    max_artifact_bytes: int = 262_144
    rate_limit_agent_per_minute: int = 120
    rate_limit_anon_per_minute: int = 600
    session_idle_timeout_seconds: int = 43_200
    session_absolute_timeout_seconds: int = 604_800
    event_retention_days: int = 30
    failed_event_retention_days: int = 90
    task_event_retention_days: int = 180
    audit_retention_days: int = 365
    github_delivery_retention_days: int = 30
    backup_dir: str = "./backups"
    backup_time_utc: str = "03:15"
    backup_retention_days: int = 14
    public_base_url: str = "http://127.0.0.1:3000"
    cookie_secure: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    allowed_origins: tuple[str, ...] = ()

    @property
    def database_path(self) -> Path:
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix) :])
        return Path("agenthub.db")

    @property
    def app_secret(self) -> bytes:
        path = Path(self.app_secret_key_file)
        if path.is_file():
            return path.read_bytes().strip()
        # Development-only deterministic fallback. Production startup checks reject it.
        return os.getenv("APP_SECRET_KEY", "agenthub-development-key-change-me").encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    hosts = tuple(
        filter(
            None,
            (
                part.strip()
                for part in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
            ),
        )
    )
    origins = tuple(
        filter(None, (part.strip() for part in os.getenv("ALLOWED_ORIGINS", "").split(",")))
    )
    return Settings(
        app_env=os.getenv("APP_ENV", "production"),
        log_level=os.getenv("LOG_LEVEL", "info"),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:////app/data/agenthub.db"),
        allow_unsafe_db_fs=_bool("ALLOW_UNSAFE_DB_FS"),
        app_secret_key_file=os.getenv("APP_SECRET_KEY_FILE", "/run/secrets/app_secret_key"),
        github_private_key_file=os.getenv(
            "GITHUB_PRIVATE_KEY_FILE", "/run/secrets/github_app_private_key"
        ),
        github_token_file=os.getenv("GITHUB_TOKEN_FILE", ""),
        github_webhook_secret_file=os.getenv(
            "GITHUB_WEBHOOK_SECRET_FILE", "/run/secrets/github_webhook_secret"
        ),
        github_app_id=os.getenv("GITHUB_APP_ID", ""),
        github_installation_id=os.getenv("GITHUB_INSTALLATION_ID", ""),
        github_repository_owner=os.getenv("GITHUB_REPOSITORY_OWNER", ""),
        github_knowledge_repository=os.getenv("GITHUB_KNOWLEDGE_REPOSITORY", "agenthub-knowledge"),
        github_default_branch=os.getenv("GITHUB_DEFAULT_BRANCH", "main"),
        github_pr_poll_interval_seconds=_int("GITHUB_PR_POLL_INTERVAL_SECONDS", 300),
        github_webhooks_enabled=_bool("GITHUB_WEBHOOKS_ENABLED"),
        publish_max_attempts=_int("PUBLISH_MAX_ATTEMPTS", 5),
        task_lease_seconds=_int("TASK_LEASE_SECONDS", 120),
        task_heartbeat_seconds=_int("TASK_HEARTBEAT_SECONDS", 40),
        task_max_attempts=_int("TASK_MAX_ATTEMPTS", 3),
        task_retry_backoff_seconds=_int("TASK_RETRY_BACKOFF_SECONDS", 30),
        task_retry_backoff_max_seconds=_int("TASK_RETRY_BACKOFF_MAX_SECONDS", 600),
        task_max_runtime_seconds=_int("TASK_MAX_RUNTIME_SECONDS", 14400),
        worker_poll_interval_seconds=_int("WORKER_POLL_INTERVAL_SECONDS", 10),
        outbox_poll_interval_seconds=_int("OUTBOX_POLL_INTERVAL_SECONDS", 5),
        outbox_processing_timeout_seconds=_int("OUTBOX_PROCESSING_TIMEOUT_SECONDS", 120),
        outbox_max_attempts=_int("OUTBOX_MAX_ATTEMPTS", 8),
        sse_max_connections=_int("SSE_MAX_CONNECTIONS", 20),
        max_request_bytes=_int("MAX_REQUEST_BYTES", 1_048_576),
        max_task_payload_bytes=_int("MAX_TASK_PAYLOAD_BYTES", 65_536),
        max_message_bytes=_int("MAX_MESSAGE_BYTES", 65_536),
        max_artifact_bytes=_int("MAX_ARTIFACT_BYTES", 262_144),
        rate_limit_agent_per_minute=_int("RATE_LIMIT_AGENT_PER_MINUTE", 120),
        rate_limit_anon_per_minute=_int("RATE_LIMIT_ANON_PER_MINUTE", 600),
        session_idle_timeout_seconds=_int("SESSION_IDLE_TIMEOUT_SECONDS", 43_200),
        session_absolute_timeout_seconds=_int("SESSION_ABSOLUTE_TIMEOUT_SECONDS", 604_800),
        event_retention_days=_int("EVENT_RETENTION_DAYS", 30),
        failed_event_retention_days=_int("FAILED_EVENT_RETENTION_DAYS", 90),
        task_event_retention_days=_int("TASK_EVENT_RETENTION_DAYS", 180),
        audit_retention_days=_int("AUDIT_RETENTION_DAYS", 365),
        github_delivery_retention_days=_int("GITHUB_DELIVERY_RETENTION_DAYS", 30),
        backup_dir=os.getenv("BACKUP_DIR", "./backups"),
        backup_time_utc=os.getenv("BACKUP_TIME_UTC", "03:15"),
        backup_retention_days=_int("BACKUP_RETENTION_DAYS", 14),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:3000"),
        cookie_secure=_bool("COOKIE_SECURE"),
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
