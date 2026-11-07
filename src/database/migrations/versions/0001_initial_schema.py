"""Create AgentHub operational schema.

Revision ID: 0001_initial
Revises:
"""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_TABLES = [
    """CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member')),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
        failed_login_count INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        last_login_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    """CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE CHECK (length(slug) BETWEEN 2 AND 63
            AND slug NOT GLOB '*[^a-z0-9-]*' AND slug NOT GLOB '-*'),
        name TEXT NOT NULL,
        description TEXT,
        instructions TEXT,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    """CREATE TABLE agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        agent_type TEXT NOT NULL CHECK (agent_type IN ('hermes','opencode','copilot','other')),
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
        execution_mode TEXT NOT NULL DEFAULT 'poll' CHECK (execution_mode IN ('poll','adapter')),
        adapter_config TEXT CHECK (adapter_config IS NULL OR json_valid(adapter_config)),
        last_seen_at TEXT,
        capabilities TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(capabilities) AND json_type(capabilities)='array'),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        csrf_secret TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_seen_at TEXT,
        idle_expires_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL,
        revoked_at TEXT,
        user_agent TEXT,
        ip_address TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE user_project_access (
        user_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('viewer','maintainer')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (user_id, project_id),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE agent_tokens (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        token_prefix TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        expires_at TEXT,
        revoked_at TEXT,
        last_used_at TEXT,
        FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE project_agents (
        project_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('reader','contributor','maintainer')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (project_id, agent_id),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        sender_agent_id TEXT,
        recipient_agent_id TEXT,
        thread_id TEXT,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        message_type TEXT NOT NULL CHECK (message_type IN ('info','question','answer','status','handoff','system')),
        idempotency_key TEXT,
        request_fingerprint TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (sender_agent_id) REFERENCES agents(id),
        FOREIGN KEY (recipient_agent_id) REFERENCES agents(id),
        FOREIGN KEY (thread_id) REFERENCES messages(id)
    )""",
    """CREATE TABLE message_receipts (
        message_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        read_at TEXT,
        acknowledged_at TEXT,
        PRIMARY KEY (message_id,agent_id),
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        created_by_agent_id TEXT NOT NULL,
        assigned_agent_id TEXT,
        target_capability TEXT,
        task_type TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        input_payload TEXT CHECK (input_payload IS NULL OR json_valid(input_payload)),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','in_progress','blocked','completed','failed','cancelled')),
        priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
        idempotency_key TEXT,
        request_fingerprint TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
        available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        lease_owner_agent_id TEXT,
        lease_expires_at TEXT,
        lease_token_hash TEXT,
        lease_instance_id TEXT,
        claimed_at TEXT,
        heartbeat_at TEXT,
        cancel_requested_at TEXT,
        blocked_reason TEXT,
        result_summary TEXT,
        error_detail TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        started_at TEXT,
        completed_at TEXT,
        final_agent_id TEXT,
        final_lease_token_hash TEXT,
        final_fingerprint TEXT,
        CHECK ((lease_owner_agent_id IS NULL) = (lease_expires_at IS NULL)),
        CHECK ((lease_owner_agent_id IS NULL) = (lease_token_hash IS NULL)),
        CHECK ((status IN ('claimed','in_progress')) = (lease_owner_agent_id IS NOT NULL)),
        CHECK ((final_agent_id IS NULL) = (final_lease_token_hash IS NULL)
            AND (final_lease_token_hash IS NULL) = (final_fingerprint IS NULL)),
        CHECK (final_fingerprint IS NULL OR status IN ('completed','failed','cancelled')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (created_by_agent_id) REFERENCES agents(id),
        FOREIGN KEY (assigned_agent_id) REFERENCES agents(id),
        FOREIGN KEY (lease_owner_agent_id) REFERENCES agents(id),
        FOREIGN KEY (final_agent_id) REFERENCES agents(id)
    )""",
    """CREATE TABLE task_events (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        actor_agent_id TEXT,
        event_type TEXT NOT NULL,
        payload TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (actor_agent_id) REFERENCES agents(id)
    )""",
    """CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT,
        created_by_agent_id TEXT,
        artifact_type TEXT NOT NULL CHECK (artifact_type IN ('research','specification','decision','implementation','review')),
        title TEXT NOT NULL,
        summary TEXT,
        document_key TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        supersedes_artifact_id TEXT,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        repository_owner TEXT NOT NULL,
        repository_name TEXT NOT NULL,
        branch_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        branch_commit_sha TEXT,
        commit_sha TEXT,
        pull_request_number INTEGER,
        pull_request_url TEXT,
        publication_status TEXT NOT NULL DEFAULT 'draft' CHECK (publication_status IN ('draft','in_review','published','rejected','superseded','failed')),
        publish_attempts INTEGER NOT NULL DEFAULT 0,
        next_publish_attempt_at TEXT,
        last_error TEXT,
        idempotency_key TEXT,
        request_fingerprint TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        published_at TEXT,
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (created_by_agent_id) REFERENCES agents(id),
        FOREIGN KEY (supersedes_artifact_id) REFERENCES artifacts(id)
    )""",
    """CREATE TABLE events_outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        project_id TEXT,
        event_type TEXT NOT NULL,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','delivered','failed','not_required')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        processing_started_at TEXT,
        delivered_at TEXT,
        last_attempt_at TEXT,
        next_attempt_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )""",
    """CREATE TABLE github_deliveries (
        delivery_id TEXT PRIMARY KEY,
        event_name TEXT NOT NULL,
        action TEXT,
        pull_request_number INTEGER,
        received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        processed_at TEXT,
        processing_status TEXT NOT NULL,
        error_detail TEXT
    )""",
    """CREATE TABLE audit_logs (
        id TEXT PRIMARY KEY,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        project_id TEXT,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        metadata TEXT CHECK (metadata IS NULL OR json_valid(metadata)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
]

_INDEXES = [
    "CREATE INDEX idx_tasks_claim ON tasks(project_id,status,priority DESC,created_at,id)",
    "CREATE INDEX idx_tasks_lease_expiry ON tasks(lease_expires_at) WHERE status IN ('claimed','in_progress')",
    "CREATE INDEX idx_tasks_assigned ON tasks(assigned_agent_id,status)",
    "CREATE UNIQUE INDEX uq_tasks_project_idempotency ON tasks(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX idx_task_events_task ON task_events(task_id,created_at)",
    "CREATE INDEX idx_project_agents_agent ON project_agents(agent_id)",
    "CREATE INDEX idx_messages_project_created ON messages(project_id,created_at,id)",
    "CREATE INDEX idx_messages_recipient ON messages(recipient_agent_id,created_at)",
    "CREATE INDEX idx_messages_thread ON messages(thread_id)",
    "CREATE UNIQUE INDEX uq_messages_sender_idempotency ON messages(project_id,sender_agent_id,idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX idx_artifacts_project_status ON artifacts(project_id,publication_status)",
    "CREATE INDEX idx_artifacts_publish_queue ON artifacts(publication_status,next_publish_attempt_at)",
    "CREATE INDEX idx_artifacts_pr ON artifacts(pull_request_number) WHERE pull_request_number IS NOT NULL",
    "CREATE UNIQUE INDEX uq_artifacts_document_version ON artifacts(project_id,artifact_type,document_key,version)",
    "CREATE UNIQUE INDEX uq_artifacts_one_open_per_document ON artifacts(project_id,artifact_type,document_key) WHERE publication_status IN ('draft','in_review')",
    "CREATE UNIQUE INDEX uq_artifacts_project_idempotency ON artifacts(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX idx_outbox_dispatch ON events_outbox(status,next_attempt_at) WHERE status IN ('pending','processing')",
    "CREATE INDEX idx_outbox_project_seq ON events_outbox(project_id,seq)",
    "CREATE INDEX idx_agent_tokens_prefix ON agent_tokens(token_prefix)",
    "CREATE INDEX idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX idx_sessions_expiry ON sessions(absolute_expires_at)",
    "CREATE INDEX idx_audit_created ON audit_logs(created_at)",
    "CREATE INDEX idx_audit_project ON audit_logs(project_id,created_at)",
    "CREATE VIRTUAL TABLE artifacts_fts USING fts5(artifact_id UNINDEXED,project_id UNINDEXED,title,summary,content,tokenize='porter unicode61')",
]


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS artifacts_fts")
    for table in (
        "audit_logs",
        "github_deliveries",
        "events_outbox",
        "artifacts",
        "task_events",
        "tasks",
        "message_receipts",
        "messages",
        "project_agents",
        "agent_tokens",
        "user_project_access",
        "sessions",
        "agents",
        "projects",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
