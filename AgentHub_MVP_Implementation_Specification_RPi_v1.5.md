# AgentHub MVP — Implementation Specification
## Raspberry Pi 4 & SQLite Edition

**Version 1.5 — Revised 7 November 2026**

**Document status:** Revised implementation specification for phased implementation  
**Target platform:** Raspberry Pi 4 (4GB RAM), Raspberry Pi OS 64-bit, Docker Compose  
**Network:** Private home network  
**Canonical knowledge repository:** Private GitHub repository  
**Database:** SQLite (WAL mode)  
**Primary agents:** Hermes Agent, OpenCode, VS Code/GitHub Copilot

---

## Changes in v1.5

This revision addresses three further review findings. Items marked **Defect** would have caused incorrect behavior if implemented as written in v1.4.

| # | Change | Section |
|---|---|---|
| 1 | **Defect:** lease ownership was identified by agent ID only, so a stale execution instance sharing an agent's credentials could heartbeat, update, complete or publish against a lease it no longer held. Added a per-claim fencing token (`lease_token`), stored only as a hash and required on every lease-scoped operation. | 5.3, 6.1, 6.3, 8.3 |
| 2 | **Defect:** completion was not idempotent. A retry after a lost response failed because the lease had already been cleared. Defined terminal-report idempotency (complete, fail, cancel acknowledgement) with explicit replay, conflict and rejection rules, stored on the task row. | 5.3, 6.3, 8.3 |
| 3 | Replaced the MCP tool table with formal JSON Schemas for every tool: inputs, outputs, errors, pagination, idempotency and permissions. Schemas are generated from shared Pydantic models and checked by a conformance test. Added the missing `get_task` tool. | 8.3 |
| 4 | Added `idempotency_key` to `publish_message` and `publish_artifact`. `publish_artifact` with a `task_id` now requires the current lease token. | 5.3, 7.2, 8.3 |
| 5 | New error codes `task_not_leased` and `completion_conflict`. Lease tokens added to the never-log list. | 8.1, 12.3 |
| 6 | New acceptance tests for fencing, terminal-report idempotency, message/artifact idempotency and schema conformance. | 15, 16 |

### Decisions made in v1.5

Override any of these before handoff if you disagree.

1. The lease token is stored only as a SHA-256 hash, and the raw token is returned once in the claim response. A claim whose response is lost cannot be recovered: the lease expires and the task is retried.
2. `instance_id` is informational only and is never used for authorization.
3. The lenient expiry rule from v1.4 is kept: an owner presenting a valid token still succeeds while the lease is expired but not yet reaped. Making expiry strict is a one-line change in the ownership check, at the cost of occasionally discarding finished work after a timing race.
4. Terminal-report idempotency covers completed, failed and cancelled reports made by the lease owner. Terminal transitions made by the reaper have no replay record.
5. Non-terminal owner reports (release, blocked, retryable failure) are not replayable. A repeat receives `403 not_lease_owner`, and the agent should re-read the task with `get_task`.
6. Output schemas are declared and `structuredContent` is returned only where the pinned MCP SDK and clients support it; the text content always carries the same JSON.

---

## Changes in v1.4 (retained for reference)

This revision resolves the issues found in review of v1.3. Items marked **Defect** would have caused incorrect behavior if implemented as written in v1.3.

| # | Change | Section |
|---|---|---|
| 1 | **Defect:** claim logic permanently bound unassigned tasks to the first claimer, so lease recovery could never hand work to a different agent. `assigned_agent_id` is now a requested target only and is never modified by a claim. Lease-state invariants are enforced with `CHECK` constraints. | 5.3, 6 |
| 2 | **Defect:** artifact content had no storage between `publish_artifact` and the GitHub commit. Added `content`, `content_sha256`, versioning and publish-retry columns. | 5.3, 7 |
| 3 | **Defect:** SSE was served by `app-core` but outbox events are produced by `worker`. SSE now tails `events_outbox` using a monotonic `seq`. Consumers of the outbox are now defined. | 5.3, 6.4, 8.8 |
| 4 | **Defect:** crashed outbox deliveries stayed `processing` forever. Added a processing timeout and recovery. | 6.4 |
| 5 | **Defect:** memory limits summed to 1.0 GB, not 1.2 GB, and were not declared in Compose. Limits are now enforced in Compose and the target is restated. | 1.1, 10 |
| 6 | Added a REST endpoint list, MCP tool schemas, a role-permission matrix, a stable error model, pagination and rate limits. | 8 |
| 7 | Resolved the repository model: one knowledge repository for the MVP. | 3, 5.3, 7 |
| 8 | Added sessions, user roles and user-project access; token format and lookup; admin-only agent provisioning; operational CLI. | 5.3, 8, 12 |
| 9 | Resolved the TLS/cookie contradiction with two explicit transport modes. Added MCP `Origin`/`Host` validation and Nginx streaming configuration. | 10, 12.3 |
| 10 | Added artifact path sanitization, content policy, versioning, merge-commit verification and reviewer-edit handling. | 7 |
| 11 | Defined the full task state machine, blocked/cancel/failure semantics, retry backoff and reaper rules. | 6.3 |
| 12 | Defined canonical timestamp format, `CHECK` constraints, JSON validation and application-maintained `updated_at`. | 5.1 |
| 13 | Migrations run only in `app-core`; worker waits on it. Added worker health check, container hardening and log limits. | 10 |
| 14 | Hermes baseline is now poll mode over MCP. Adapter dispatch is a wake-up hint only; adapter safety rules added. | 9 |
| 15 | Added retention/pruning, WAL checkpointing, startup checks, backup CLI and restore procedure. | 12 |
| 16 | Expanded and corrected acceptance tests; reordered phases around a thin vertical slice. | 14–16 |

### Decisions made in v1.4

These were open questions in v1.3. Override any of them before handoff if you disagree.

1. **One knowledge repository** (`agenthub-knowledge`) with a `projects/<slug>/` prefix; no per-project repositories.
2. **Nginx (`web`) is the single published entry point** for the dashboard, `/api/v1` and `/mcp`. `app-core` is published only on `127.0.0.1` for health checks and debugging.
3. **Polling is the primary GitHub reconciliation path.** Webhooks are an optional accelerator.
4. **No local Git clone in the MVP.** Content is staged in SQLite and verified against GitHub through the API.
5. **Hermes is integrated as a polling MCP client first.** Adapter push is optional and only after the execution mechanism is verified.
6. **Dashboard users cannot create tasks.** Tasks are created by agents; dashboard maintainers may cancel and retry.

---

# 1. Purpose and overview

Build **AgentHub**, a self-hosted coordination service that allows AI agents to exchange messages, delegate research and implementation tasks, publish durable project knowledge, and retrieve prior work.

The primary workflow is:

1. Hermes Agent researches a project or question.
2. Hermes publishes findings to AgentHub.
3. AgentHub stores operational metadata in SQLite and submits durable knowledge to a private GitHub repository.
4. OpenCode or a coding agent using VS Code/GitHub Copilot retrieves the research and implements the work.
5. A coding agent can create a research task for Hermes.
6. Hermes works on that task in the background and publishes its result back to AgentHub.
7. Users inspect agents, tasks, messages, artifacts, and publication status through a web dashboard.

## 1.1 Raspberry Pi 4 optimization

The system must be lightweight enough to operate reliably on a Raspberry Pi 4 with 4GB RAM.

Key design decisions:

- Consolidate the REST API and MCP server into one FastAPI application process.
- Use one Uvicorn worker initially.
- Use SQLite rather than a separate database server.
- Run one background worker process initially.
- Keep model inference outside AgentHub containers.
- Use WAL mode and short SQLite write transactions.
- Prefer a reliable USB SSD over an SD card for database storage.
- Enforce hard container memory limits totalling 1.0 GB (Section 10.3) and target a measured typical working set below 800 MB.

SQLite's `synchronous=NORMAL` setting reduces write overhead but does not eliminate storage wear or guarantee survival of sudden power loss. Backups remain necessary.

---

# 2. Goals and non-goals

## 2.1 MVP goals

- One central AgentHub service deployed using Docker Compose.
- Unified backend handling REST API (`/api/v1`) and MCP Streamable HTTP (`/mcp`).
- SQLite with WAL mode as the operational source of truth.
- Private GitHub repository as the canonical durable knowledge store.
- GitHub branch-and-pull-request workflow for publishing knowledge.
- Agent registration and project membership.
- Messaging, task creation, claiming, leases, heartbeats, retries, and audit history.
- Lightweight React/TypeScript dashboard.
- Hermes background-task integration as the first autonomous adapter.
- OpenCode and VS Code/Copilot integration through MCP.
- Secure-by-default deployment for a private home network.

## 2.2 Non-goals

- Multi-tenant SaaS support.
- Running heavy model inference inside AgentHub containers.
- Replacing GitHub Issues or local Git repositories.
- High-frequency transactional workloads exceeding approximately 100 requests per second.
- Distributed database clustering.
- Redis, a vector database, or additional infrastructure services for the MVP.

---

# 3. Core design decisions

| Concern | MVP decision |
|---|---|
| Backend | Python 3.12+ and FastAPI |
| Backend process | One consolidated API + MCP application (`app-core`) and one `worker` |
| Agent protocol | Official MCP Python SDK, Streamable HTTP, stateless mode, bearer-token authentication |
| Database | SQLite 3.35+ in WAL mode; FTS5 and JSON1 required |
| Database access | SQLAlchemy 2.x and `aiosqlite`; SQLite-specific writes behind a repository layer |
| Migrations | Alembic, executed only by `app-core` at startup |
| Frontend | React + TypeScript, built off the Pi, served by Nginx |
| Entry point | Nginx `web` container: dashboard, `/api/v1`, `/mcp` |
| Transport security | TLS recommended; explicit plain-HTTP LAN mode otherwise |
| Deployment | Docker Compose with enforced memory limits |
| Knowledge store | Single private GitHub repository (`agenthub-knowledge`) |
| GitHub authentication | GitHub App preferred; fine-grained PAT acceptable for initial MVP |
| Artifact publication | Content staged in SQLite; dedicated branch and pull request |
| Background execution | One Python worker process running independent async loops |
| First agent integration | Hermes as a polling MCP client; adapter push optional |
| Search | SQLite FTS5 over published artifacts |
| Event delivery | Durable event log (`events_outbox`), SSE tailing the log, authenticated polling |
| GitHub reconciliation | Polling primary; webhooks optional |
| Network security | Private LAN binding; VPN for remote access |

**Operational source of truth:** SQLite owns task state, messages, agent registrations, audit history, and publication metadata.

**Knowledge source of truth:** GitHub owns durable research, specifications, decisions, and other published project artifacts.

**Derived data:** The FTS5 index is derived from `artifacts` and must be rebuildable at any time with the `reindex` command.

---

# 4. System architecture

## 4.1 Docker services

Run three primary containers. `app-core` and `worker` communicate only through SQLite. There is no direct RPC between them; worker poll intervals bound latency.

### `app-core`

Consolidated FastAPI application hosting:

- REST API.
- MCP Streamable HTTP endpoint.
- Authentication and authorization (agent tokens and browser sessions).
- SSE event endpoint, implemented by tailing `events_outbox`.
- GitHub webhook receiver (optional accelerator).
- Health checks.

Startup sequence (`deploy/entrypoint-app.sh`):

1. Run `alembic upgrade head`. Abort startup if migration fails.
2. Run startup checks (Section 12.4).
3. `exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'`

`app-core` never calls GitHub and never receives the GitHub App private key. In-memory state is limited to caches, the SSE subscriber registry and rate-limit counters; no correctness property may depend on it.

### `worker`

One process running independent async loops. Each loop has its own error handling so one failing loop cannot stop the others:

| Loop | Responsibility |
|---|---|
| Lease reaper | Recover expired task leases (Section 6.3) |
| Outbox dispatcher | Deliver wake-up hints to adapter-mode agents; recover stuck `processing` events (Section 6.4) |
| Artifact publisher | Create branches, commits and pull requests for `draft` artifacts (Section 7.2) |
| GitHub reconciler | Poll open pull requests and reconcile artifact state (Section 7.4) |
| Indexer | Maintain FTS5 entries as artifacts change state |
| Maintenance | Retention pruning, WAL checkpoints, scheduled backups (Section 12) |

The worker writes a heartbeat file on every loop iteration for the container health check. The worker waits for `app-core` to be healthy before starting, so migrations are never run concurrently.

The GitHub App private key is mounted only into this container.

### `web`

Nginx serving the static React build and reverse-proxying `/api/v1` and `/mcp` to `app-core`. Streaming endpoints require specific proxy settings (Section 10.2). Optionally terminates TLS (Section 10.4).

## 4.2 Logical architecture

```text
Hermes / OpenCode / VS Code Copilot          Human browser
                 |                                  |
        MCP Streamable HTTP / REST            HTTPS or HTTP
                 \                                  /
                  +------------ web (Nginx) -------+
                                   |
                               app-core
                               FastAPI
                  (REST, MCP, auth, SSE, webhook receiver)
                                   |
                                SQLite  <------------------+
                 tasks, messages, artifacts, events_outbox |
                                   |                       |
                                 worker  -------------------+
          reaper | outbox | publisher | reconciler | indexer | maintenance
                       |                    |
                 Agent adapters         GitHub API
              (wake-up hints only)    (branch, commit, PR, merge verification)

GitHub webhooks (optional) ----------------> app-core --> same reconcile function
```

## 4.3 Storage responsibilities

| Storage | Responsibility |
|---|---|
| SQLite | Operational data, staged artifact content, FTS5 index, event log |
| GitHub private repository | Canonical durable knowledge |
| Docker volume `sqlite_data` | Persistent SQLite database and WAL files |
| Backup directory | Local SQLite backups, synchronized off the Pi where practical |

There is no local Git clone in the MVP. Artifact content is staged in `artifacts.content`, written to GitHub through the API, and verified against GitHub at the merge commit before it is indexed. If a clone is needed later for very large repositories, add it as a separate, documented change.

The SQLite database must reside on a local filesystem-backed Docker volume. Do not place it on SMB, NFS, or another network filesystem. The application enforces this at startup (Section 12.4).

---

# 5. Database and domain model

## 5.1 Database configuration

Use:

- SQLAlchemy 2.x.
- `aiosqlite` for async database operations.
- Alembic for schema migrations.
- UUID strings as primary keys.
- UTC timestamps stored consistently.
- Foreign keys enabled.
- Explicit indexes for common queries.

Alembic migrations must be the sole authoritative schema definition. Do not maintain a separate hand-edited `schema.sql` that can drift from migrations.

### SQLite connection initialization

Every connection must enable foreign keys and configure its busy timeout and connection-local settings.

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -16000;
```

Set WAL journal mode once during database initialization and verify it during startup:

```sql
PRAGMA journal_mode = WAL;
```

The application must handle SQLite's single-writer behavior correctly. WAL permits readers and a writer to operate concurrently, but it does not allow multiple simultaneous writers.

### Transaction requirements

- Keep write transactions short.
- Never perform network calls inside a database transaction.
- Retry `SQLITE_BUSY` and database-locked errors using bounded exponential backoff with jitter.
- Use a dedicated repository implementation for SQLite-specific operations.
- Do not mix ad-hoc transaction statements with implicit SQLAlchemy transaction management.
- Test the exact SQLAlchemy/`aiosqlite` transaction implementation used for `BEGIN IMMEDIATE`.

### Data conventions

- **Timestamps:** All timestamps are stored as UTC text in one canonical, fixed-width format: `YYYY-MM-DDTHH:MM:SS.mmmZ` (millisecond precision, for example `2026-11-06T14:03:22.481Z`). Implement this with a SQLAlchemy `TypeDecorator`. The canonical format sorts correctly as a string and is index-friendly, so queries compare against an application-supplied `:now` parameter instead of wrapping columns in `datetime()`. Never mix this format with SQLite's `CURRENT_TIMESTAMP` output (`YYYY-MM-DD HH:MM:SS`), which compares incorrectly against it.
- **DDL shorthand:** In the SQL below, `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` is shorthand. In Alembic migrations, declare these as `TEXT` with `server_default` `(strftime('%Y-%m-%dT%H:%M:%fZ','now'))`. The application normally supplies explicit values.
- **`updated_at`:** There are no triggers. The repository layer must set `updated_at` in every `UPDATE`.
- **Identifiers:** UUID strings. Public-facing references to projects may also use the project slug.
- **Booleans:** `INTEGER` 0/1 with a `CHECK`.
- **JSON columns:** `TEXT` with `CHECK (col IS NULL OR json_valid(col))`. Application code validates structure and size.
- **Enumerations:** Enforced with `CHECK` constraints as shown. Use Alembic batch mode for migrations that change them.
- **Size limits:** Enforced in the application (Section 11), not by SQLite.
- **Deletion:** Prefer `is_active`/status flags. Hard-deleting a project cascades to its tasks, messages and artifacts and is an admin-only, audited operation.

### Required SQLite transaction implementation

Every write path, not just task claiming, must acquire the write lock up front. A deferred transaction that reads and then writes can fail immediately with `SQLITE_BUSY` when it tries to upgrade, without honoring the busy timeout. Using `BEGIN IMMEDIATE` makes contention surface at `BEGIN`, where a retry is always safe.

Recommended implementation:

1. On each new DBAPI connection, disable the driver's implicit transaction handling (`isolation_level = None`, or the equivalent autocommit mode for the pinned `aiosqlite`/Python version) and apply the connection PRAGMAs.
2. Provide a single repository helper, `write_tx()`, as an async context manager that issues `BEGIN IMMEDIATE`, yields a connection, then issues `COMMIT`, or `ROLLBACK` on any exception.
3. All state-changing repository functions use `write_tx()`. Do not use the ORM unit of work for write paths that need immediate locking; use SQLAlchemy Core or raw SQL through the helper.
4. Reads run outside `write_tx()`. Multi-statement reads that need a consistent snapshot use a single deferred `BEGIN ... COMMIT`.
5. Do not hold a read transaction open between polls (for example in the SSE tailer). Long-lived readers prevent WAL checkpointing.
6. Retry only when acquiring the lock fails (`database is locked` at `BEGIN IMMEDIATE`), never after statements have run in the transaction. Use up to 5 attempts with exponential backoff and jitter starting at 50 ms.

Verify the transaction behavior against the pinned SQLAlchemy and `aiosqlite` versions with a two-process contention test (Section 16).

## 5.2 Core tables

The database must contain the following tables.

| Table | Purpose |
|---|---|
| `users` | Human dashboard accounts and roles |
| `sessions` | Browser sessions |
| `user_project_access` | Dashboard user access to projects |
| `agents` | Registered AI agents |
| `agent_tokens` | Hashed agent authentication tokens |
| `projects` | Project definitions and instructions |
| `project_agents` | Agent membership and project roles |
| `messages` | Agent-to-agent and project messages |
| `message_receipts` | Message read/acknowledgement state |
| `tasks` | Task queue and task execution state |
| `task_events` | Task lifecycle history |
| `artifacts` | Staged content and GitHub publication metadata |
| `events_outbox` | Ordered durable event log and delivery queue |
| `github_deliveries` | Webhook delivery deduplication and processing |
| `audit_logs` | Security and administrative audit history |
| `artifacts_fts` | FTS5 virtual table (Section 5.5) |

## 5.3 Schema definitions

### Users

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('admin', 'member')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP,
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Admins have implicit maintainer access to every project. Passwords use Argon2id.

### Sessions

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,              -- SHA-256 hex of the opaque session token
    user_id TEXT NOT NULL,
    csrf_secret TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP,
    idle_expires_at TIMESTAMP NOT NULL,
    absolute_expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP,
    user_agent TEXT,
    ip_address TEXT,
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);
```

The raw session token exists only in the browser cookie. Update `last_seen_at` and `idle_expires_at` at most once per minute per session to avoid write amplification.

### User project access

```sql
CREATE TABLE user_project_access (
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'maintainer')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, project_id),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE
);
```

Dashboard `viewer`: read-only. Dashboard `maintainer`: may cancel and retry tasks and view unpublished artifacts for that project. Non-admin users see only projects they have access to.

### Agents

```sql
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    agent_type TEXT NOT NULL
        CHECK (agent_type IN ('hermes', 'opencode', 'copilot', 'other')),
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    execution_mode TEXT NOT NULL DEFAULT 'poll'
        CHECK (execution_mode IN ('poll', 'adapter')),
    adapter_config TEXT
        CHECK (adapter_config IS NULL OR json_valid(adapter_config)),
    last_seen_at TIMESTAMP,
    capabilities TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(capabilities)
               AND json_type(capabilities) = 'array'),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `capabilities` is a JSON array of lowercase strings such as `"research"` or `"implementation"`. It is used for `target_capability` task routing.
- `adapter_config` must never contain secrets; reference secrets by file path or secret name.
- Agents are created and disabled only by an admin (CLI or dashboard). Disabling an agent takes effect immediately for authentication and releases its active leases (tasks return to `pending` through the normal reaper transitions).
- Update `last_seen_at` on authenticated requests at most once per 30 seconds per agent.

### Agent tokens

```sql
CREATE TABLE agent_tokens (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    revoked_at TIMESTAMP,
    last_used_at TIMESTAMP,
    FOREIGN KEY (agent_id)
        REFERENCES agents(id) ON DELETE CASCADE
);
```

Only token hashes are stored. The raw token is displayed once when generated.

Token format: `ah_<prefix>_<secret>`, where `<prefix>` is 8 random URL-safe characters and `<secret>` is at least 32 bytes of CSPRNG output, URL-safe encoded. Store the lowercase hex SHA-256 of the full token in `token_hash` and the prefix in `token_prefix`. Authentication looks up candidate rows by `token_prefix` (indexed), then compares hashes with a constant-time comparison. SHA-256 is appropriate here because the secret is high-entropy; Argon2 is reserved for human passwords. Reject revoked and expired tokens and tokens of disabled agents. Update `last_used_at` at most once per minute.

### Projects

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE
        CHECK (length(slug) BETWEEN 2 AND 63
               AND slug NOT GLOB '*[^a-z0-9-]*'
               AND slug NOT GLOB '-*'),
    name TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `instructions` is the project overview and working instructions exposed through the MCP project resource.
- All projects publish to the single configured knowledge repository under `projects/<slug>/`. Per-project repositories are out of scope for the MVP; adding them later is a schema migration.

### Project agents

```sql
CREATE TABLE project_agents (
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, agent_id),
    FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id)
        REFERENCES agents(id) ON DELETE CASCADE
);
```

Supported roles:

- `reader`
- `contributor`
- `maintainer`

### Messages

```sql
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sender_agent_id TEXT,
    recipient_agent_id TEXT,
    thread_id TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    message_type TEXT NOT NULL
        CHECK (message_type IN
               ('info', 'question', 'answer', 'status', 'handoff', 'system')),
    idempotency_key TEXT,
    request_fingerprint TEXT,      -- SHA-256 of canonical request payload
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (sender_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (recipient_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (thread_id)
        REFERENCES messages(id)
);
```

Rules:

- A null `sender_agent_id` means a system message. A null `recipient_agent_id` means a project broadcast.
- `thread_id` is the `id` of the root message of the thread. A reply must be in the same project as its root, and the application sets `thread_id` to the root's id even when replying to a non-root message.
- Sender and recipient must both be members of the project.
- Idempotency: `publish_message` accepts an optional `idempotency_key`, unique per `(project_id, sender_agent_id)`. A repeat with the same key and an identical request fingerprint returns the existing message with `idempotent_replay: true`. A repeat with a different fingerprint fails with `409 idempotency_conflict`.
- Body size is limited by `MAX_MESSAGE_BYTES`.

### Message receipts

```sql
CREATE TABLE message_receipts (
    message_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    read_at TIMESTAMP,
    acknowledged_at TIMESTAMP,
    PRIMARY KEY (message_id, agent_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id)
        REFERENCES agents(id) ON DELETE CASCADE
);
```

Receipt semantics (lazy receipts):

- For a direct message, create the recipient's receipt row when the message is created.
- For a broadcast, do **not** create receipts at send time. A broadcast is unread for an agent when the agent is a project member, the message was created at or after the agent's `project_agents.created_at`, the sender is not that agent, and the agent has no receipt row with `read_at` set.
- Create or update the receipt row when an agent reads or acknowledges a message. This avoids fan-out writes and handles agents added to a project later.

### Tasks

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_by_agent_id TEXT NOT NULL,
    assigned_agent_id TEXT,        -- requested target agent; never modified by a claim
    target_capability TEXT,        -- optional capability routing
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    input_payload TEXT
        CHECK (input_payload IS NULL OR json_valid(input_payload)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'in_progress', 'blocked',
                          'completed', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0
        CHECK (priority BETWEEN -100 AND 100),
    idempotency_key TEXT,
    request_fingerprint TEXT,      -- SHA-256 of canonical creation payload
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK (max_attempts BETWEEN 1 AND 10),
    available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_owner_agent_id TEXT,
    lease_expires_at TIMESTAMP,
    lease_token_hash TEXT,         -- SHA-256 of the current lease's fencing token
    lease_instance_id TEXT,        -- optional; informational only, never used for authorization
    claimed_at TIMESTAMP,          -- set on every claim; bounds max runtime
    heartbeat_at TIMESTAMP,
    cancel_requested_at TIMESTAMP,
    blocked_reason TEXT,
    result_summary TEXT,
    error_detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,          -- first claim
    completed_at TIMESTAMP,
    final_agent_id TEXT,           -- owner whose report made the task terminal
    final_lease_token_hash TEXT,   -- lease token hash of that report
    final_fingerprint TEXT,        -- SHA-256 of that report's canonical payload
    CHECK ((lease_owner_agent_id IS NULL) = (lease_expires_at IS NULL)),
    CHECK ((lease_owner_agent_id IS NULL) = (lease_token_hash IS NULL)),
    CHECK ((status IN ('claimed', 'in_progress'))
           = (lease_owner_agent_id IS NOT NULL)),
    CHECK ((final_agent_id IS NULL) = (final_lease_token_hash IS NULL)
           AND (final_lease_token_hash IS NULL) = (final_fingerprint IS NULL)),
    CHECK (final_fingerprint IS NULL
           OR status IN ('completed', 'failed', 'cancelled')),
    FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (assigned_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (lease_owner_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (final_agent_id)
        REFERENCES agents(id)
);
```

Field semantics:

- `assigned_agent_id` is the agent the creator asked to perform the task. It is set only at creation (or by a maintainer reassignment) and is **never** changed by a claim or by lease recovery. If null, any eligible agent may claim the task.
- `target_capability`, if set, restricts claiming to agents whose `capabilities` array contains that value.
- `lease_owner_agent_id`, `lease_expires_at` and `lease_token_hash` identify the current lease generation. The `CHECK` constraints guarantee that a lease exists exactly when the status is `claimed` or `in_progress`, and that owner, expiry and token hash are always set or cleared together. Every transition out of those states must clear all lease fields (including `lease_instance_id`).
- `lease_token_hash` fences each lease generation so that two execution instances sharing one agent identity can be told apart (Section 6.3). The raw token is never stored.
- `final_agent_id`, `final_lease_token_hash` and `final_fingerprint` record the lease owner's report that made the task terminal, so a repeated report can be recognized (Section 6.3). They are set and cleared together, are null for terminal transitions made by the reaper or by cancellation without acknowledgement, and are cleared when a maintainer retries a failed task.

Task statuses and the allowed transitions are defined in Section 6.3.

Idempotency: a task may be created with an `idempotency_key`, unique within a project when present:

```sql
CREATE UNIQUE INDEX uq_tasks_project_idempotency
ON tasks(project_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

Creating a task with an existing key and an identical `request_fingerprint` returns the existing task and marks the response `idempotent_replay: true`. Creating a task with an existing key and a different fingerprint fails with `409 idempotency_conflict`.

### Task events

```sql
CREATE TABLE task_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    actor_agent_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id)
        REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_agent_id)
        REFERENCES agents(id)
);
```

Record every task transition, claim, heartbeat, lease expiry, retry, and completion.

### Artifacts

```sql
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT,
    created_by_agent_id TEXT,
    artifact_type TEXT NOT NULL
        CHECK (artifact_type IN ('research', 'specification', 'decision',
                                 'implementation', 'review')),
    title TEXT NOT NULL,
    summary TEXT,
    document_key TEXT NOT NULL,        -- stable logical document identifier
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_artifact_id TEXT,
    content TEXT NOT NULL,             -- agent-supplied Markdown body (no front matter)
    content_sha256 TEXT NOT NULL,      -- SHA-256 of the body as last verified
    repository_owner TEXT NOT NULL,    -- snapshot of configured repository
    repository_name TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    branch_commit_sha TEXT,            -- commit on the artifact branch
    commit_sha TEXT,                   -- commit on the default branch at which
                                       -- merged content was verified
    pull_request_number INTEGER,
    pull_request_url TEXT,
    publication_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft', 'in_review', 'published',
                                      'rejected', 'superseded', 'failed')),
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    next_publish_attempt_at TIMESTAMP,
    last_error TEXT,
    idempotency_key TEXT,
    request_fingerprint TEXT,          -- SHA-256 of canonical request payload
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP,
    FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id)
        REFERENCES tasks(id),
    FOREIGN KEY (created_by_agent_id)
        REFERENCES agents(id),
    FOREIGN KEY (supersedes_artifact_id)
        REFERENCES artifacts(id)
);
```

Publication states:

| State | Meaning |
|---|---|
| `draft` | Content staged in SQLite; the worker has not yet opened a pull request |
| `in_review` | Pull request is open |
| `published` | Pull request merged and content verified on the default branch |
| `rejected` | Pull request closed without merging |
| `superseded` | A newer version of the same document has been published |
| `failed` | Publishing failed permanently (see `last_error`) |

Allowed transitions: `draft → in_review`, `draft → failed`, `in_review → published`, `in_review → rejected`, `rejected → in_review` (pull request reopened), `published → superseded`. All other transitions are invalid.

Versioning rules:

- A logical document is identified by `(project_id, artifact_type, document_key)`.
- Publishing when a `draft` or `in_review` version of the same document exists fails with `409 artifact_open_version_exists`.
- Otherwise the new artifact receives `version = max(existing versions) + 1` and sets `supersedes_artifact_id` to the latest `published` version, if any. That version becomes `superseded` (and is removed from FTS) only when the new version is verified as merged.

Idempotency: `publish_artifact` accepts an optional `idempotency_key`, unique within a project. A repeat with the same key and an identical request fingerprint returns the existing artifact, whatever its current status, with `idempotent_replay: true`. A repeat with a different fingerprint fails with `409 idempotency_conflict`. The idempotency check runs before the open-version check, so a retry after a lost response never produces `artifact_open_version_exists`.

The uniqueness and idempotency rules are enforced by indexes (Section 5.4).

### Durable event log and outbox

```sql
CREATE TABLE events_outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'delivered',
                          'failed', 'not_required')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processing_started_at TIMESTAMP,
    delivered_at TIMESTAMP,
    last_attempt_at TIMESTAMP,
    next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
```

This table serves two purposes:

1. **Ordered event log.** Every state change inserts an event in the same transaction as the change. The monotonically increasing `seq` lets SSE clients and polling clients resume from a cursor. `app-core` reads the log for SSE regardless of `status`.
2. **Delivery queue for active consumers.** The only active consumers in the MVP are agent adapters, which receive wake-up hints (Section 9). An event is inserted with status `pending` only if at least one active adapter-mode agent should be notified. Otherwise it is inserted as `not_required`.

Event types: `task.created`, `task.available`, `task.claimed`, `task.updated`, `task.completed`, `task.failed`, `task.cancelled`, `task.cancel_requested`, `task.lease_expired`, `message.created`, `artifact.created`, `artifact.in_review`, `artifact.published`, `artifact.rejected`, `artifact.superseded`, `artifact.failed`.

Event payloads contain identifiers and status only, never message bodies, task payloads or artifact content. Clients fetch details through authorized endpoints.

Outbox statuses and recovery rules are in Section 6.4. Delivery is at-least-once; consumers must deduplicate using the event `id`.

### GitHub deliveries

```sql
CREATE TABLE github_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    processing_status TEXT NOT NULL,
    error_detail TEXT
);
```

### Audit logs

```sql
CREATE TABLE audit_logs (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    project_id TEXT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

The `metadata` column must never contain secrets, tokens, message bodies or artifact content. Use actor type `system` and actor id `system` for worker-initiated actions.

## 5.4 Performance and integrity indexes

Create these through Alembic migrations.

```sql
-- tasks
CREATE INDEX idx_tasks_claim
ON tasks(project_id, status, priority DESC, created_at);

CREATE INDEX idx_tasks_lease_expiry
ON tasks(lease_expires_at)
WHERE status IN ('claimed', 'in_progress');

CREATE INDEX idx_tasks_assigned
ON tasks(assigned_agent_id, status);

-- task_events and membership
CREATE INDEX idx_task_events_task
ON task_events(task_id, created_at);

CREATE INDEX idx_project_agents_agent
ON project_agents(agent_id);

-- messages
CREATE INDEX idx_messages_project_created
ON messages(project_id, created_at);

CREATE INDEX idx_messages_recipient
ON messages(recipient_agent_id, created_at);

CREATE INDEX idx_messages_thread
ON messages(thread_id);

-- artifacts
CREATE INDEX idx_artifacts_project_status
ON artifacts(project_id, publication_status);

CREATE INDEX idx_artifacts_publish_queue
ON artifacts(publication_status, next_publish_attempt_at);

CREATE INDEX idx_artifacts_pr
ON artifacts(pull_request_number)
WHERE pull_request_number IS NOT NULL;

CREATE UNIQUE INDEX uq_artifacts_document_version
ON artifacts(project_id, artifact_type, document_key, version);

CREATE UNIQUE INDEX uq_artifacts_one_open_per_document
ON artifacts(project_id, artifact_type, document_key)
WHERE publication_status IN ('draft', 'in_review');

CREATE UNIQUE INDEX uq_artifacts_project_idempotency
ON artifacts(project_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX uq_messages_sender_idempotency
ON messages(project_id, sender_agent_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

-- events
CREATE INDEX idx_outbox_dispatch
ON events_outbox(status, next_attempt_at)
WHERE status IN ('pending', 'processing');

CREATE INDEX idx_outbox_project_seq
ON events_outbox(project_id, seq);

-- authentication
CREATE INDEX idx_agent_tokens_prefix
ON agent_tokens(token_prefix);

CREATE INDEX idx_sessions_user
ON sessions(user_id);

CREATE INDEX idx_sessions_expiry
ON sessions(absolute_expires_at);

-- audit
CREATE INDEX idx_audit_created
ON audit_logs(created_at);

CREATE INDEX idx_audit_project
ON audit_logs(project_id, created_at);
```

Because timestamps use the canonical fixed-width format (Section 5.1), range predicates such as `available_at <= :now` can use these indexes directly.

## 5.5 FTS5 knowledge search

FTS5 uses its own integer rowid. The artifact UUID and project ID are stored as unindexed text columns.

```sql
CREATE VIRTUAL TABLE artifacts_fts USING fts5(
    artifact_id UNINDEXED,
    project_id UNINDEXED,
    title,
    summary,
    content,
    tokenize = 'porter unicode61'
);
```

Indexing requirements:

- Index only the latest `published` version of each document.
- Insert or replace the FTS row in the **same database transaction** that sets the artifact to `published`. Remove it in the same transaction that sets `rejected` or `superseded`, or when an artifact is deleted. Locate rows with `WHERE artifact_id = ?`; this is acceptable at MVP scale, and a mapping table may be added later.
- Index the Markdown body only, excluding the AgentHub-generated front matter.
- Store the source GitHub commit SHA (`artifacts.commit_sha`) with the artifact.
- Provide a `reindex` command that rebuilds `artifacts_fts` from the `artifacts` table.
- Verify at startup that the SQLite build supports FTS5 and JSON1; refuse to start otherwise.

Search requirements:

- Every search is scoped to projects the caller can read. Filter by `project_id` and join `artifacts` for authorization and metadata.
- Return artifact ID, project, repository, file path, version, commit SHA and pull request URL with each result, ordered by `bm25()`, with `snippet()` for display.
- **Never pass client input directly to `MATCH`.** Split the input into terms, wrap each term in double quotes (doubling any embedded double quote), and join with `AND`. Reject empty queries. FTS operators and column filters supplied by clients must be treated as literal text.
- Draft and in-review artifacts are not in FTS5. When a caller with permission requests unpublished results (`include_unpublished`), search `title` and `summary` in the `artifacts` table with an escaped `LIKE`. Contributors see only their own unpublished artifacts; maintainers see all in the project; readers see none.

---

# 6. Task engine and concurrency

## 6.1 Atomic task claiming

Claiming runs inside one short `BEGIN IMMEDIATE` transaction (`write_tx()`, Section 5.1), so eligibility checks, task selection, the lease update, the task event and the outbox event are committed atomically.

Illustrative pseudocode (production code must use the repository abstraction and the pinned SQLAlchemy/`aiosqlite` implementation):

```python
def claim_task(db, agent_id: str, project_id: str,
               task_id: str | None = None,
               instance_id: str | None = None) -> dict | None:
    now = utc_now()                                   # canonical format
    expires = utc_now(plus_seconds=cfg.TASK_LEASE_SECONDS)
    lease_token = new_lease_token()                   # CSPRNG, >= 128 bits
    token_hash = sha256_hex(lease_token)              # only the hash is stored

    with db.write_tx() as tx:                         # BEGIN IMMEDIATE ... COMMIT
        eligible = tx.execute("""
            SELECT 1
            FROM project_agents pa
            JOIN agents a   ON a.id = pa.agent_id
            JOIN projects p ON p.id = pa.project_id
            WHERE pa.project_id = :project_id
              AND pa.agent_id   = :agent_id
              AND pa.role IN ('contributor', 'maintainer')
              AND a.status = 'active'
              AND p.is_active = 1
        """, {"project_id": project_id, "agent_id": agent_id}).fetchone()
        if eligible is None:
            raise Forbidden("insufficient_role")

        row = tx.execute("""
            SELECT t.id
            FROM tasks t
            WHERE t.project_id = :project_id
              AND t.status = 'pending'
              AND t.available_at <= :now
              AND t.cancel_requested_at IS NULL
              AND (t.assigned_agent_id IS NULL
                   OR t.assigned_agent_id = :agent_id)
              AND (t.target_capability IS NULL
                   OR EXISTS (
                       SELECT 1
                       FROM agents a2, json_each(a2.capabilities) c
                       WHERE a2.id = :agent_id
                         AND c.value = t.target_capability))
              AND (:task_id IS NULL OR t.id = :task_id)
            ORDER BY t.priority DESC, t.created_at ASC
            LIMIT 1
        """, {"project_id": project_id, "agent_id": agent_id,
              "now": now, "task_id": task_id}).fetchone()
        if row is None:
            return None

        claimed = tx.execute("""
            UPDATE tasks
            SET status               = 'claimed',
                lease_owner_agent_id = :agent_id,
                lease_expires_at     = :expires,
                lease_token_hash     = :token_hash,
                lease_instance_id    = :instance_id,
                claimed_at           = :now,
                heartbeat_at         = :now,
                attempt_count        = attempt_count + 1,
                started_at           = COALESCE(started_at, :now),
                blocked_reason       = NULL,
                updated_at           = :now
            WHERE id = :id AND status = 'pending'
        """, {"agent_id": agent_id, "expires": expires,
              "token_hash": token_hash, "instance_id": instance_id,
              "now": now, "id": row["id"]}).rowcount
        if claimed != 1:
            return None   # defensive; cannot happen under BEGIN IMMEDIATE

        record_task_event(tx, row["id"], agent_id, "claimed", now,
                          lease_fp=token_hash[:8])
        emit_event(tx, "task.claimed", "task", row["id"], project_id, now)

    # read after commit; the raw token is returned here and nowhere else
    return fetch_task_with_context(db, row["id"], lease_token=lease_token)
```

Notes:

- `assigned_agent_id` is deliberately **not** modified. This is what allows a task whose lease expired to be claimed by a different eligible agent.
- If `task_id` is supplied and the task is not claimable, the API layer returns `409 task_not_claimable` (or `404 not_found` if the caller cannot see the task).
- Never make network calls inside `write_tx()`.
- The raw `lease_token` exists only in memory during the claim and in the claim response. The database stores its hash, so a database or backup leak cannot be used to impersonate a lease holder.
- A claim whose response is lost in transit cannot be recovered, because the raw token is gone. The lease expires and the task is retried. Keep `TASK_LEASE_SECONDS` short for this reason.

The claim response must include:

- Task ID, task type, title and description.
- Input payload.
- Project context (slug, name, instructions).
- Attempt count and maximum attempts.
- The lease token (returned only in this response), lease expiry and the recommended heartbeat interval.
- Whether cancellation has been requested.
- References to relevant published artifacts (the task's linked artifacts and the most recent published artifacts in the project).

## 6.2 Claiming requirements

1. Only active members of an active project with role `contributor` or `maintainer` may claim project tasks. `reader` members may not.
2. The agent's own status must be `active`.
3. A task with `assigned_agent_id` set may only be claimed by that agent.
4. A task with `target_capability` set may only be claimed by an agent whose `capabilities` include it.
5. The task must be `pending`, available (`available_at <= now`) and have no pending cancellation request.
6. A claim sets `status`, `lease_owner_agent_id`, `lease_expires_at`, `lease_token_hash`, `lease_instance_id`, `claimed_at`, `heartbeat_at` and `attempt_count` consistently in one statement.
7. The attempt count increments only on a successful claim.
8. A task with a lease cannot be claimed by another agent.
9. Claim order is `priority DESC, created_at ASC`.

## 6.3 State machine, leases and heartbeats

### Defaults

| Setting | Default | Variable |
|---|---:|---|
| Task lease | 120 s | `TASK_LEASE_SECONDS` |
| Heartbeat interval (client) | 40 s | `TASK_HEARTBEAT_SECONDS` |
| Maximum attempts | 3 | `TASK_MAX_ATTEMPTS` |
| Retry backoff | 30 s × 2^(attempt − 1), capped at 600 s | `TASK_RETRY_BACKOFF_SECONDS`, `TASK_RETRY_BACKOFF_MAX_SECONDS` |
| Maximum runtime per attempt | 4 h | `TASK_MAX_RUNTIME_SECONDS` |
| Reaper / worker poll | 10 s | `WORKER_POLL_INTERVAL_SECONDS` |
| Outbox poll | 5 s | `OUTBOX_POLL_INTERVAL_SECONDS` |
| Recommended minimum agent poll interval | 10 s | (documented, rate-limited) |

### Allowed transitions

Define these in one table in application code and reject everything else with `409 invalid_transition`. Every transition writes a `task_events` row and an `events_outbox` row in the same transaction.

| From | To | Triggered by | Notes |
|---|---|---|---|
| `pending` | `claimed` | `claim_task` | Eligibility per 6.2 |
| `pending` | `cancelled` | Cancel | Immediate |
| `claimed` | `in_progress` | Lease owner `update_task` | First progress report |
| `claimed`, `in_progress` | `completed` | Lease owner `complete_task` | Clears lease; sets `completed_at` |
| `claimed`, `in_progress` | `pending` | Lease owner releases or reports a retryable failure with attempts remaining; or lease expiry with attempts remaining | Clears lease; sets `available_at` using retry backoff |
| `claimed`, `in_progress` | `failed` | Lease owner reports a non-retryable failure, or a retryable failure with attempts exhausted; or lease expiry with attempts exhausted | Clears lease; records `error_detail` |
| `claimed`, `in_progress` | `blocked` | Lease owner `update_task` with a reason | Releases the lease (see below) |
| `claimed`, `in_progress` | `cancelled` | Lease owner acknowledges cancellation; or reaper when `cancel_requested_at` is set and the lease expires | Clears lease |
| `blocked` | `pending` | Task creator or maintainer unblocks | `available_at = now`; `attempt_count` unchanged |
| `blocked` | `cancelled` | Cancel | Immediate |
| `failed` | `pending` | Maintainer retry | Resets `attempt_count` to 0 |
| `completed`, `cancelled` | — | — | Terminal |

Every transition triggered by the lease owner requires a valid lease token (see Lease fencing token below), and terminal owner reports are idempotent (see Terminal report idempotency below).

"Attempts exhausted" means `attempt_count >= max_attempts`. A voluntary release or a reported retryable failure consumes the attempt that was just made.

### Lease fencing token

An agent ID alone cannot identify an executor. Two execution instances can share one agent's credentials: for example a restarted process, and an older one that has not yet noticed it lost its lease. Without a fence, the older instance could submit stale work after the task was reclaimed. Every claim therefore generates a fresh **lease token** that fences that lease generation.

- The token is generated inside the claim transaction from a CSPRNG (at least 128 bits, URL-safe). The database stores only its SHA-256 hash in `lease_token_hash`. The raw token is returned once, in the claim response.
- The caller presents it as `lease_token` on `heartbeat_task`, `update_task` (owner transitions), `complete_task`, the cancellation acknowledgement, and `publish_artifact` when `task_id` is given.
- An owner operation succeeds only if **all** of the following hold: the caller's agent ID equals `lease_owner_agent_id`, the task is `claimed` or `in_progress`, and the hash of the presented token equals `lease_token_hash` (constant-time comparison). Otherwise the response is `403 not_lease_owner`. The error never reveals whether the task is leased, by whom, or what its result is.
- Every transition out of `claimed` or `in_progress` clears the token hash. Each new claim creates a new token and tokens are never reused. A stale instance holding an old token is rejected even though its agent ID equals the current owner's.
- `instance_id`, which the agent may supply at claim, is stored for logging and dashboard display only. It must never be used for authorization.
- Ownership checks run inside `BEGIN IMMEDIATE` transactions, so they are serialized with the reaper and with other claims.
- If a lease has expired but the reaper has not yet processed the task, the owner presenting a valid token **still succeeds**. The reaper acts only on rows it finds expired when it runs. Once the task has been reaped and possibly re-claimed, the old token no longer matches. To make expiry strict instead, reject expired leases in the ownership check. This specification keeps the lenient rule so that finished work is not discarded by a timing race.
- A heartbeat extends `lease_expires_at` to `now + lease` but never beyond `claimed_at + TASK_MAX_RUNTIME_SECONDS`. After that bound the lease is allowed to expire normally. The heartbeat response includes `lease_expires_at` and `cancel_requested`, and never returns the token.
- Unblocking, maintainer retry and cancellation requests are not lease operations and do not use the token.
- If an agent process crashes it loses its token and cannot resume the lease. The lease expires and the task is retried, consuming an attempt.
- Audit: `task_events` rows record a short token fingerprint (the first 8 hex characters of the hash, never the token). Rejected stale-writer attempts are recorded in `audit_logs` as `task.stale_lease_rejected`, at most once per minute per task and agent.

### Terminal report idempotency

Responses can be lost. An agent that completes a task and does not receive the response will repeat the call, and by then the lease has been cleared. The repeat must neither fail spuriously nor change the result.

Covered reports: `complete_task`, `update_task` with status `failed`, and `update_task` with status `cancelled` (the acknowledgement of a cancellation request), when made by the lease owner.

**Fingerprint.** SHA-256 of canonical JSON (UTF-8, sorted keys, no insignificant whitespace) of:

- Completion: `{"kind":"complete","result_summary":<string>,"artifact_ids":<sorted, de-duplicated list>}`
- Failure: `{"kind":"fail","error_detail":<string>,"retryable":<bool>}`
- Cancel acknowledgement: `{"kind":"cancel"}`

`progress_note` is not part of the fingerprint.

**Recording.** When a terminal owner report is applied, store `final_agent_id`, `final_lease_token_hash` and `final_fingerprint` on the task row in the same transaction. A maintainer retry (`failed` to `pending`) clears them. Terminal transitions made by the reaper, or by cancellation without acknowledgement, leave them null, so there is nothing to replay.

**Decision order**, evaluated inside one `write_tx()`:

| # | Condition | Response |
|---|---|---|
| 1 | Task is leased; agent ID and token hash match the lease | Apply the transition and store the `final_*` fields |
| 2 | `final_*` is set; the caller's agent ID and token hash match it; same kind of report; identical fingerprint | `200` with the current task and `idempotent_replay: true`. No state change, no `task_events`, no outbox events, no audit entry |
| 3 | `final_*` is set; the caller's agent ID and token hash match it; same kind of report; different fingerprint | `409 completion_conflict` |
| 4 | `final_*` is set; the caller's agent ID and token hash match it; a different kind of report (for example `complete_task` after a recorded failure) | `409 invalid_transition` |
| 5 | Anything else: a stale token, a token cleared by the reaper or a re-claim, another agent, or no `final_*` record | `403 not_lease_owner` |

Properties:

- The replay record lives on the `tasks` row, not in `task_events`, so retention pruning of events never breaks a replay. It persists as long as the task row exists.
- `artifact_ids` in a completion must name artifacts in the same project whose `task_id` equals this task. Otherwise the request fails with `422 validation_error` and nothing is applied.
- Non-terminal owner reports (voluntary release, `blocked`, and a retryable failure that returns the task to `pending`) are not replayable. The token is cleared by the transition, so a repeat receives `403 not_lease_owner`. An agent that receives that response must stop work, discard its result and call `get_task` to see the current state rather than retry blindly.
- A completion report from a stale lease generation is always rejected (row 5), including after another instance has completed the task.

### Blocked tasks

A task reported as `blocked` releases its lease. Blocked tasks may wait on a human for hours, and requiring heartbeats would be impractical. The task keeps its `assigned_agent_id` and `blocked_reason`. Only the task creator or a maintainer can move it back to `pending`. The next claim increments `attempt_count` as usual.

### Cancellation

- Cancelling a `pending` or `blocked` task moves it to `cancelled` immediately.
- Cancelling a `claimed` or `in_progress` task sets `cancel_requested_at` and emits `task.cancel_requested`. The lease owner learns of it through the heartbeat or update response and should stop work and acknowledge by setting the status to `cancelled`, presenting the lease token. If the lease expires first, the reaper moves the task to `cancelled`, not back to `pending`.
- Who may cancel: dashboard admins and maintainers, project maintainers (agents), and a contributor for tasks they created.

### Lease reaper

The worker runs the reaper every `WORKER_POLL_INTERVAL_SECONDS`:

1. In a short transaction, select up to 50 tasks with `status IN ('claimed','in_progress')` and `lease_expires_at < :now`.
2. For each task, in the same transaction, apply the first matching rule:
   1. `cancel_requested_at` set → `cancelled`.
   2. Attempts exhausted → `failed` with `error_detail = 'lease expired; attempts exhausted'`.
   3. Otherwise → `pending` with `available_at = now + backoff`.
3. Clear all lease fields (owner, expiry, token hash and instance ID), write a `task_events` row of type `lease_expired`, write the resulting transition event, and emit `task.lease_expired` plus `task.available` where applicable.
4. Repeat until no expired tasks remain, using a separate transaction per batch.

The reaper uses the same `write_tx()` helper and the same transition table as the API.

## 6.4 Outbox delivery

The outbox dispatcher in the worker handles events with `status = 'pending'`:

1. In a short `write_tx()`, select a small batch (default 20) of events with `status = 'pending'` and `next_attempt_at <= :now`, ordered by `seq`. Set `status = 'processing'`, `processing_started_at = :now`, `last_attempt_at = :now` and increment `attempt_count`. Commit.
2. Perform delivery outside the transaction: resolve the target adapter-mode agents and call the adapter's `dispatch` (Section 9). Delivery is a wake-up hint only.
3. On success, set `status = 'delivered'` and `delivered_at`.
4. On failure, record `last_error` and set `status = 'pending'` with `next_attempt_at` computed by bounded exponential backoff (5 s × 2^attempt, capped at 300 s, with jitter).
5. After `OUTBOX_MAX_ATTEMPTS` (default 8) attempts, set `status = 'failed'`. Failed events are visible in the dashboard.

Recovery: on every loop iteration, reset events stuck in `processing` for longer than `OUTBOX_PROCESSING_TIMEOUT_SECONDS` (default 120) back to `pending`, preserving `attempt_count`. This makes a worker crash during delivery safe, at the cost of a possible duplicate hint, which is why consumers must deduplicate by event `id`.

Event-log retention is defined in Section 12.4. SSE and polling clients read the log by `seq` (Section 8.8).

---

# 7. GitHub knowledge repository workflow

## 7.1 Repository structure

Create a private GitHub repository named:

`agenthub-knowledge`

Recommended layout:

```text
agenthub-knowledge/
├── projects/
│   └── <project-slug>/
│       ├── research/
│       ├── specifications/
│       ├── decisions/
│       ├── implementation/
│       └── reviews/
├── shared/
│   ├── standards/
│   ├── architecture/
│   └── conventions/
├── templates/
│   ├── research.md
│   ├── specification.md
│   ├── decision.md
│   └── review.md
└── .github/
    ├── CODEOWNERS
    └── pull_request_template.md
```

## 7.2 Publication workflow

Publication is asynchronous. `publish_artifact` returns as soon as the artifact is staged.

1. **Stage.** An agent with role `contributor` or `maintainer` invokes `publish_artifact`. `app-core` applies the idempotency check (a repeated `idempotency_key` with an identical request returns the existing artifact; a different request returns `409 idempotency_conflict`) and, when `task_id` is supplied, the lease check (the task must be `claimed` or `in_progress` and the caller must present its current `lease_token`; otherwise `403 not_lease_owner` or `409 task_not_leased`). It then validates the request (Section 7.5), resolves the document version, computes the target path, and inserts the artifact row with `publication_status = 'draft'`, `content`, `content_sha256` and `branch_name = agenthub/<project-slug>/<artifact-id>`. It appends an `artifact.created` event and returns the artifact ID.
2. **Pick up.** The worker's artifact publisher selects due `draft` artifacts (`next_publish_attempt_at` null or past), increments `publish_attempts` and sets `next_publish_attempt_at` a few minutes ahead in a short transaction, so a crashed attempt is retried automatically.
3. **Push to GitHub (outside any transaction).** Every step must be idempotent:
   1. Read the default branch HEAD.
   2. Create the artifact branch, or reuse it if it already exists.
   3. Write the file with AgentHub-generated front matter followed by the artifact body. Record the resulting commit as `branch_commit_sha`.
   4. Open the pull request against the default branch, or reuse the existing pull request found by head branch.
4. **Record.** In a short transaction, set `in_review`, `pull_request_number`, `pull_request_url` and `branch_commit_sha`, and emit `artifact.in_review`. If the worker crashes after step 3 and before step 4, the retry finds the existing branch and pull request and completes the record without duplicating anything.
5. **Review.** A human or authorized reviewer reviews and merges the pull request on GitHub.
6. **Reconcile.** The reconciler (Section 7.4) fetches the pull request from GitHub:
   - **Merged:** fetch the file at the pull request's merge commit SHA, strip the front matter, and compare the body hash to `content_sha256`. If it differs (the reviewer edited the file), accept GitHub's content as authoritative: update `content` and `content_sha256` and write an audit entry noting the change. Then, in one transaction, set `published`, `commit_sha` to the merge commit SHA, `published_at`, mark the previously published version `superseded` and remove it from FTS, and insert or replace this artifact's FTS row.
   - **Closed without merge:** set `rejected` and remove any FTS row.
   - **Open:** no change. If the artifact was `rejected` and the pull request has been reopened, return it to `in_review`.
7. **Failure.** Retry transient failures (network errors, 5xx, rate limits) with bounded exponential backoff, honoring `Retry-After` and rate-limit reset headers. After `PUBLISH_MAX_ATTEMPTS` (default 5) attempts, or on a non-retryable error such as a missing repository, set `failed` with `last_error` and emit `artifact.failed`.

Never mark an artifact `published` before the pull request is verified as merged on GitHub. Content changes made directly on the default branch outside this workflow are not detected automatically; the `reindex --refresh` command re-fetches published artifacts at the default branch HEAD and records any difference.

### Target path and front matter

The target path is derived by AgentHub, never supplied by the agent:

`projects/<project-slug>/<type-directory>/<document_key>.md`

| `artifact_type` | Directory |
|---|---|
| `research` | `research` |
| `specification` | `specifications` |
| `decision` | `decisions` |
| `implementation` | `implementation` |
| `review` | `reviews` |

`document_key` must match `^[a-z0-9][a-z0-9._-]{0,79}$` and must not contain consecutive dots. If the agent omits it, derive it by slugifying the title. Agent-supplied directories, file extensions and path separators are rejected.

AgentHub prepends YAML front matter containing the artifact ID, project slug, type, version, creating agent name, task ID (if any) and creation time. Agent content that begins with its own front matter block is rejected so agents cannot spoof this metadata.

## 7.3 GitHub authentication

Preferred: GitHub App.

Permissions:

- Contents: read/write.
- Metadata: read.
- Pull requests: read/write.

Webhook subscriptions are optional. If webhooks are enabled, subscribe only to:

- `pull_request`
- `ping`

Use short-lived installation access tokens, cached in worker memory and refreshed before expiry. The private key is mounted only into the worker container.

A fine-grained PAT restricted to the single knowledge repository (Contents and Pull requests read/write) is acceptable for the initial MVP. A GitHub App is preferred long term.

## 7.4 Reconciliation: polling first, webhooks optional

A private home-network Pi is usually unreachable from GitHub, so polling is the primary and guaranteed mechanism. Webhooks are an accelerator for setups that expose a webhook route through a tunnel.

**Polling mode (required)**

- The worker polls every `GITHUB_PR_POLL_INTERVAL_SECONDS` (default 300) for artifacts in `in_review`, and additionally once on startup.
- It also re-checks recently `rejected` artifacts so reopened pull requests are noticed.
- It handles API rate limits with backoff and conditional requests (ETags) where practical.

**Webhook mode (optional)**

- Validate `X-Hub-Signature-256` using a constant-time comparison.
- Verify the repository identity and the expected event and action.
- Deduplicate deliveries using `X-GitHub-Delivery` and the `github_deliveries` table.
- Process only the configured repository.
- A valid webhook does not carry state into the database directly. It triggers the same reconcile function for the affected pull request, which fetches state from GitHub.

Both paths call the same idempotent `reconcile_pull_request` function. GitHub is authoritative for whether a pull request has been merged.

## 7.5 Content policy and repository protection

`publish_artifact` must reject, with `422 validation_error` or `422 content_policy_violation`:

- Content that is not valid UTF-8, contains NUL bytes, or exceeds `MAX_ARTIFACT_BYTES` (default 262144).
- Titles longer than 200 characters or containing control characters.
- Invalid `document_key` values (Section 7.2).
- Content containing high-confidence secret patterns, at minimum: PEM private key blocks, GitHub tokens (`ghp_`, `github_pat_`, `ghs_`), AWS access key IDs, and AgentHub agent tokens (`ah_`-prefixed). Rejection responses must not echo the matched text.

Repository protection on the knowledge repository:

- Protect the default branch: require pull requests and at least one approving review. Because pull requests are authored by the GitHub App, the repository owner can approve them.
- Do not grant the GitHub App permission to bypass branch protection.
- Add a `CODEOWNERS` file naming the maintainer.

---

# 8. API and MCP contract

## 8.1 Endpoints and architecture rule

All external traffic enters through the `web` container (Section 10):

```text
<PUBLIC_BASE_URL>/api/v1/     REST API
<PUBLIC_BASE_URL>/mcp         MCP Streamable HTTP
<PUBLIC_BASE_URL>/            Dashboard
```

`PUBLIC_BASE_URL` is, for example, `https://agenthub.example.ts.net` or `http://192.168.1.10:3000`.

**Single authorization path.** REST route handlers and MCP tool handlers are thin adapters over the same service layer. Authentication resolves a principal (agent or user), the service layer enforces project membership, role and ownership, and no business logic or authorization lives in route or tool handlers. This is how REST and MCP stay consistent.

### REST endpoints

Project path parameters accept either the project ID or slug. Unless noted, endpoints require authentication (agent bearer token or user session).

| Area | Method and path | Notes |
|---|---|---|
| Health | `GET /health` | Unauthenticated liveness; returns only `{"status":"ok"}` |
| Health | `GET /health/details` | Admin only: DB, worker heartbeat age, queue depths, WAL size |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Browser sessions |
| Admin | `GET/POST /users`, `PATCH /users/{id}` | Admin only |
| Admin | `GET/POST /agents`, `PATCH /agents/{id}` | Admin only to create or change; `POST` returns the raw token once |
| Admin | `POST /agents/{id}/tokens`, `DELETE /agents/{id}/tokens/{token_id}` | Rotate / revoke |
| Admin | `GET/POST /projects`, `PATCH /projects/{id}` | Admin only to create or change |
| Admin | `GET/PUT/DELETE /projects/{id}/members/{agent_id}` | Agent project roles |
| Admin | `GET/PUT/DELETE /projects/{id}/users/{user_id}` | Dashboard user access |
| Agents | `GET /projects/{project}/agents`, `GET /agents/{id}` | Members visible to project readers |
| Messages | `GET/POST /projects/{project}/messages` | Cursor pagination |
| Messages | `POST /messages/{id}/read` | Body: `{"acknowledge": bool}` |
| Tasks | `GET/POST /projects/{project}/tasks` | `POST` honors `idempotency_key` |
| Tasks | `POST /projects/{project}/tasks/claim` | Body: optional `task_id` and `instance_id`; returns the task with its `lease_token`, or `204` |
| Tasks | `GET /tasks/{id}`, `GET /tasks/{id}/events` | |
| Tasks | `PATCH /tasks/{id}` | Status and progress updates |
| Tasks | `POST /tasks/{id}/heartbeat`, `/complete`, `/cancel`, `/retry` | `heartbeat` and `complete` require `lease_token` in the body |
| Artifacts | `GET/POST /projects/{project}/artifacts` | `POST` stages content for publication |
| Artifacts | `GET /artifacts/{id}` | `include_content` query flag |
| Search | `GET /projects/{project}/search?q=` | FTS5 (Section 5.5) |
| Events | `GET /events/stream` | SSE (Section 8.8) |
| Events | `GET /events?after_seq=&limit=` | Polling fallback |
| Audit | `GET /audit` | Admin only |
| Webhook | `POST /webhooks/github` | Optional; signature-authenticated, no session |

### Conventions

- JSON request and response bodies. Timestamps use the canonical format (Section 5.1).
- Cursor pagination: `limit` (default 25, maximum 100) and an opaque `cursor`; responses include `next_cursor` or null.
- Lease-scoped REST calls (`heartbeat`, `complete`, `PATCH /tasks/{id}` owner transitions, and `POST` artifacts with a `task_id`) carry `lease_token` in the JSON body, never in the URL or query string. It must be redacted from logs.
- Request size limits: `MAX_REQUEST_BYTES` overall, `MAX_TASK_PAYLOAD_BYTES` for task payloads (default 65536), `MAX_MESSAGE_BYTES` for message bodies (default 65536), `MAX_ARTIFACT_BYTES` for artifact content.

### Error model

All errors use one envelope, and codes are stable:

```json
{"error": {"code": "invalid_transition", "message": "...", "details": {}}}
```

| HTTP | Codes |
|---:|---|
| 400 | `bad_request` |
| 401 | `unauthenticated` (include `WWW-Authenticate: Bearer`) |
| 403 | `forbidden`, `insufficient_role`, `not_lease_owner`, `csrf_failed` |
| 404 | `not_found` (also returned when the caller may not know the resource exists) |
| 409 | `invalid_transition`, `task_not_claimable`, `task_not_leased`, `completion_conflict`, `idempotency_conflict`, `artifact_open_version_exists` |
| 413 | `payload_too_large` |
| 422 | `validation_error`, `content_policy_violation` |
| 429 | `rate_limited` (include `Retry-After`) |
| 503 | `unavailable` (for example when SQLite retries are exhausted; include `Retry-After`) |

MCP tools return the same codes in the tool result's error content with `isError` set.

### Rate limits

In-memory sliding-window limits are sufficient for the MVP: by default 120 requests per minute per agent token and 600 per minute per client address for unauthenticated routes, with `429` and `Retry-After`. Failed logins are limited to 5 per 15 minutes per email and address combination, after which the account is locked until `locked_until`.

## 8.2 Role and permission matrix

Agent project roles are `reader`, `contributor` and `maintainer`. Administrative operations (creating agents, tokens, projects and memberships) are available only to admin users through the dashboard, REST or CLI, never to agents.

| Operation | reader | contributor | maintainer |
|---|:---:|:---:|:---:|
| List project agents, read agent status | ✓ | ✓ | ✓ |
| Read messages and mark own receipts | ✓ | ✓ | ✓ |
| Publish messages | — | ✓ | ✓ |
| List and read tasks | ✓ | ✓ | ✓ |
| Create tasks | — | ✓ | ✓ |
| Claim tasks; heartbeat, update, complete (own lease, with lease token) | — | ✓ | ✓ |
| Cancel tasks | — | own created tasks | ✓ |
| Unblock tasks | — | own created tasks | ✓ |
| Retry failed tasks | — | — | ✓ |
| Publish artifacts | — | ✓ | ✓ |
| Read and search published artifacts | ✓ | ✓ | ✓ |
| Read unpublished artifacts | — | own | ✓ |

Dashboard users: `viewer` is read-only for the project; `maintainer` and admin users may additionally cancel and retry tasks. Dashboard users cannot create tasks, messages or artifacts.

A non-member receives `404 not_found` for project-scoped resources.

## 8.3 MCP tool contracts

This section is the machine-readable contract for every MCP tool. The same models back the REST API (Section 8.1), so a REST request and the equivalent MCP call validate identically.

### 8.3.1 Conventions

**Schemas.** JSON Schema draft 2020-12. Every tool input schema sets `additionalProperties: false`, so unknown fields are rejected with `422 validation_error`. Character limits in schemas are lower-bound guards. The server additionally enforces the byte limits in Section 8.1 (`MAX_MESSAGE_BYTES`, `MAX_TASK_PAYLOAD_BYTES`, `MAX_ARTIFACT_BYTES`) and returns `413 payload_too_large`.

**Errors.** A failed tool call returns an MCP tool result with `isError: true`. Its `structuredContent` (where supported) and its text content both carry the `ErrorEnvelope` below. Authentication failures are HTTP `401` before MCP processing begins. JSON-RPC protocol errors are used only for malformed requests and unknown tools. The common errors, which any tool can return, are `forbidden`, `insufficient_role`, `not_found`, `validation_error`, `payload_too_large`, `rate_limited` and `unavailable`. Section 8.3.4 lists the tool-specific codes.

**Pagination.** Keyset pagination with an opaque `cursor`. A cursor is bound to the filters of the request that produced it, and reusing it with different filters returns `422 validation_error`. `next_cursor` is `null` on the last page. Ordering is stable and deterministic, with `id` as the final tiebreaker. A tool either paginates (and returns `next_cursor`) or returns a bounded result set and has no cursor, as stated in Section 8.3.4.

**Idempotency.** Tools that create state accept an `idempotency_key`. A repeat with the same key and an identical request returns the existing object with `idempotent_replay: true`. A repeat with the same key and a different request returns `409 idempotency_conflict`. Lease-scoped terminal reports follow Section 6.3. Read tools and heartbeat are naturally idempotent.

**Lease tokens.** Every lease-scoped operation takes the `lease_token` returned by `claim_task` (Section 6.3). The token is never returned by any other tool.

**Project references.** Wherever `project` appears it accepts the project slug or its lowercase UUID.

### 8.3.2 Shared definitions

`common.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agenthub.local/schemas/common.json",
  "$defs": {
    "Uuid": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"},
    "NullableUuid": {"oneOf": [{"$ref": "#/$defs/Uuid"}, {"type": "null"}]},
    "ProjectRef": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$", "description": "Project slug or lowercase UUID"},
    "Timestamp": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$"},
    "NullableTimestamp": {"oneOf": [{"$ref": "#/$defs/Timestamp"}, {"type": "null"}]},
    "NullableString": {"type": ["string", "null"]},
    "Cursor": {"type": "string", "minLength": 1, "maxLength": 512},
    "Limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    "LeaseToken": {"type": "string", "minLength": 22, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"},
    "IdempotencyKey": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9._:-]+$"},
    "Capability": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"},
    "TaskType": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"},
    "DocumentKey": {"type": "string", "pattern": "^(?!.*\\.\\.)[a-z0-9][a-z0-9._-]{0,79}$"},
    "TaskStatus": {"enum": ["pending", "claimed", "in_progress", "blocked", "completed", "failed", "cancelled"]},
    "ArtifactType": {"enum": ["research", "specification", "decision", "implementation", "review"]},
    "PublicationStatus": {"enum": ["draft", "in_review", "published", "rejected", "superseded", "failed"]},
    "MessageType": {"enum": ["info", "question", "answer", "status", "handoff", "system"]},
    "ProjectRole": {"enum": ["reader", "contributor", "maintainer"]},
    "ErrorEnvelope": {
      "type": "object", "required": ["error"], "additionalProperties": false,
      "properties": {"error": {
        "type": "object", "required": ["code", "message"], "additionalProperties": false,
        "properties": {
          "code": {"type": "string"},
          "message": {"type": "string"},
          "details": {"type": "object"}}}}
    }
  }
}
```

`entities.json` (output objects). `$ref` values beginning `common.json#` refer to the file above.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agenthub.local/schemas/entities.json",
  "$defs": {
    "AgentProfile": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "name", "agent_type", "status", "capabilities"],
      "properties": {
        "id": {"$ref": "common.json#/$defs/Uuid"},
        "name": {"type": "string"},
        "agent_type": {"enum": ["hermes", "opencode", "copilot", "other"]},
        "description": {"$ref": "common.json#/$defs/NullableString"},
        "status": {"enum": ["active", "disabled"]},
        "capabilities": {"type": "array", "items": {"$ref": "common.json#/$defs/Capability"}},
        "last_seen_at": {"$ref": "common.json#/$defs/NullableTimestamp"}}
    },
    "AgentMember": {
      "allOf": [{"$ref": "#/$defs/AgentProfile"}],
      "required": ["role"],
      "properties": {"role": {"$ref": "common.json#/$defs/ProjectRole"}}
    },
    "Membership": {
      "type": "object", "additionalProperties": false, "required": ["project", "role"],
      "properties": {"project": {"type": "string"}, "role": {"$ref": "common.json#/$defs/ProjectRole"}}
    },
    "Message": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "project", "subject", "body", "message_type", "created_at"],
      "properties": {
        "id": {"$ref": "common.json#/$defs/Uuid"},
        "project": {"type": "string"},
        "sender_agent_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "recipient_agent_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "thread_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "message_type": {"$ref": "common.json#/$defs/MessageType"},
        "created_at": {"$ref": "common.json#/$defs/Timestamp"},
        "read_at": {"$ref": "common.json#/$defs/NullableTimestamp"},
        "acknowledged_at": {"$ref": "common.json#/$defs/NullableTimestamp"}}
    },
    "Receipt": {
      "type": "object", "additionalProperties": false, "required": ["message_id", "agent_id"],
      "properties": {
        "message_id": {"$ref": "common.json#/$defs/Uuid"},
        "agent_id": {"$ref": "common.json#/$defs/Uuid"},
        "read_at": {"$ref": "common.json#/$defs/NullableTimestamp"},
        "acknowledged_at": {"$ref": "common.json#/$defs/NullableTimestamp"}}
    },
    "TaskSummary": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "project", "task_type", "title", "status", "priority", "attempt_count", "available_at", "created_at"],
      "properties": {
        "id": {"$ref": "common.json#/$defs/Uuid"},
        "project": {"type": "string"},
        "task_type": {"$ref": "common.json#/$defs/TaskType"},
        "title": {"type": "string"},
        "status": {"$ref": "common.json#/$defs/TaskStatus"},
        "priority": {"type": "integer", "minimum": -100, "maximum": 100},
        "assigned_agent_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "target_capability": {"$ref": "common.json#/$defs/NullableString"},
        "attempt_count": {"type": "integer", "minimum": 0},
        "available_at": {"$ref": "common.json#/$defs/Timestamp"},
        "created_at": {"$ref": "common.json#/$defs/Timestamp"}}
    },
    "Task": {
      "allOf": [{"$ref": "#/$defs/TaskSummary"}],
      "required": ["max_attempts", "cancel_requested", "created_by_agent_id", "updated_at"],
      "properties": {
        "description": {"$ref": "common.json#/$defs/NullableString"},
        "input_payload": {},
        "created_by_agent_id": {"$ref": "common.json#/$defs/Uuid"},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        "lease_owner_agent_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "lease_expires_at": {"$ref": "common.json#/$defs/NullableTimestamp"},
        "cancel_requested": {"type": "boolean"},
        "blocked_reason": {"$ref": "common.json#/$defs/NullableString"},
        "result_summary": {"$ref": "common.json#/$defs/NullableString"},
        "error_detail": {"$ref": "common.json#/$defs/NullableString"},
        "updated_at": {"$ref": "common.json#/$defs/Timestamp"},
        "started_at": {"$ref": "common.json#/$defs/NullableTimestamp"},
        "completed_at": {"$ref": "common.json#/$defs/NullableTimestamp"}}
    },
    "ClaimedTask": {
      "allOf": [{"$ref": "#/$defs/Task"}],
      "required": ["lease", "context"],
      "properties": {
        "lease": {
          "type": "object", "additionalProperties": false,
          "required": ["lease_token", "lease_expires_at", "heartbeat_interval_seconds"],
          "properties": {
            "lease_token": {"$ref": "common.json#/$defs/LeaseToken"},
            "lease_expires_at": {"$ref": "common.json#/$defs/Timestamp"},
            "heartbeat_interval_seconds": {"type": "integer", "minimum": 1}}},
        "context": {
          "type": "object", "additionalProperties": false, "required": ["project"],
          "properties": {
            "project": {
              "type": "object", "required": ["slug", "name"],
              "properties": {"slug": {"type": "string"}, "name": {"type": "string"},
                             "instructions": {"$ref": "common.json#/$defs/NullableString"}}},
            "artifacts": {"type": "array", "items": {"$ref": "#/$defs/ArtifactSummary"}}}}}
    },
    "ArtifactSummary": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "project", "artifact_type", "title", "document_key", "version", "publication_status", "file_path", "created_at"],
      "properties": {
        "id": {"$ref": "common.json#/$defs/Uuid"},
        "project": {"type": "string"},
        "task_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "created_by_agent_id": {"$ref": "common.json#/$defs/NullableUuid"},
        "artifact_type": {"$ref": "common.json#/$defs/ArtifactType"},
        "title": {"type": "string"},
        "summary": {"$ref": "common.json#/$defs/NullableString"},
        "document_key": {"$ref": "common.json#/$defs/DocumentKey"},
        "version": {"type": "integer", "minimum": 1},
        "publication_status": {"$ref": "common.json#/$defs/PublicationStatus"},
        "repository": {"type": "string", "description": "owner/name"},
        "file_path": {"type": "string"},
        "commit_sha": {"$ref": "common.json#/$defs/NullableString"},
        "pull_request_number": {"type": ["integer", "null"]},
        "pull_request_url": {"$ref": "common.json#/$defs/NullableString"},
        "created_at": {"$ref": "common.json#/$defs/Timestamp"},
        "published_at": {"$ref": "common.json#/$defs/NullableTimestamp"}}
    },
    "Artifact": {
      "allOf": [{"$ref": "#/$defs/ArtifactSummary"}],
      "properties": {
        "content": {"type": "string", "description": "Present only when include_content is true and the caller may read it"},
        "last_error": {"$ref": "common.json#/$defs/NullableString"}}
    },
    "SearchResult": {
      "type": "object", "additionalProperties": false,
      "required": ["artifact_id", "project", "artifact_type", "title", "snippet", "version", "publication_status", "repository", "file_path"],
      "properties": {
        "artifact_id": {"$ref": "common.json#/$defs/Uuid"},
        "project": {"type": "string"},
        "artifact_type": {"$ref": "common.json#/$defs/ArtifactType"},
        "title": {"type": "string"},
        "snippet": {"type": "string"},
        "version": {"type": "integer"},
        "publication_status": {"$ref": "common.json#/$defs/PublicationStatus"},
        "repository": {"type": "string"},
        "file_path": {"type": "string"},
        "commit_sha": {"$ref": "common.json#/$defs/NullableString"},
        "pull_request_url": {"$ref": "common.json#/$defs/NullableString"},
        "score": {"type": "number"}}
    }
  }
}
```

### 8.3.3 Tool schemas

Each block gives the tool's input schema. The output schema follows it in one line. `E` abbreviates `https://agenthub.local/schemas/entities.json#/$defs/` and `C` abbreviates `https://agenthub.local/schemas/common.json#/$defs/`.

#### `register_agent`

```json
{"type": "object", "additionalProperties": false,
 "properties": {
   "description": {"type": "string", "maxLength": 1000},
   "capabilities": {"type": "array", "items": {"$ref": "C/Capability"}, "maxItems": 32, "uniqueItems": true}}}
```

Output: `{agent: E/AgentProfile, memberships: [E/Membership]}` (both required). Updates the caller's own profile only; it never creates agents.

#### `get_agent_status`

```json
{"type": "object", "additionalProperties": false,
 "properties": {"agent_id": {"$ref": "C/Uuid", "description": "Defaults to the caller"}}}
```

Output: `{agent: E/AgentProfile, active_leases: [{task_id, project, lease_expires_at}]}`. Leases are listed only for projects the caller can read.

#### `list_agents`

```json
{"type": "object", "additionalProperties": false, "required": ["project"],
 "properties": {"project": {"$ref": "C/ProjectRef"}}}
```

Output: `{agents: [E/AgentMember]}` (required). Bounded at 200 members; no cursor.

#### `publish_message`

```json
{"type": "object", "additionalProperties": false,
 "required": ["project", "subject", "body", "message_type"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "subject": {"type": "string", "minLength": 1, "maxLength": 200},
   "body": {"type": "string", "minLength": 1, "maxLength": 65536},
   "message_type": {"$ref": "C/MessageType"},
   "recipient_agent_id": {"$ref": "C/Uuid"},
   "thread_id": {"$ref": "C/Uuid"},
   "idempotency_key": {"$ref": "C/IdempotencyKey"}}}
```

Output: `{message: E/Message, idempotent_replay: boolean}` (both required). Agents may not send `system` messages; that type is reserved for the server.

#### `get_messages`

```json
{"type": "object", "additionalProperties": false, "required": ["project"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "unread_only": {"type": "boolean", "default": false},
   "thread_id": {"$ref": "C/Uuid"},
   "since": {"$ref": "C/Timestamp"},
   "limit": {"$ref": "C/Limit"},
   "cursor": {"$ref": "C/Cursor"}}}
```

Output: `{messages: [E/Message], next_cursor: string|null}` (both required). Reading does not mark messages read.

#### `mark_message_read`

```json
{"type": "object", "additionalProperties": false, "required": ["message_id"],
 "properties": {
   "message_id": {"$ref": "C/Uuid"},
   "acknowledge": {"type": "boolean", "default": false}}}
```

Output: `{receipt: E/Receipt}`. Idempotent: `read_at` is set once, and `acknowledged_at` can change only from null to set.

#### `create_task`

```json
{"type": "object", "additionalProperties": false,
 "required": ["project", "task_type", "title"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "task_type": {"$ref": "C/TaskType"},
   "title": {"type": "string", "minLength": 1, "maxLength": 200},
   "description": {"type": "string", "maxLength": 20000},
   "input_payload": {"type": "object"},
   "assigned_agent_id": {"$ref": "C/Uuid"},
   "target_capability": {"$ref": "C/Capability"},
   "priority": {"type": "integer", "minimum": -100, "maximum": 100, "default": 0},
   "available_at": {"$ref": "C/Timestamp"},
   "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
   "idempotency_key": {"$ref": "C/IdempotencyKey"}}}
```

Output: `{task: E/Task, idempotent_replay: boolean}` (both required). `input_payload` is limited to `MAX_TASK_PAYLOAD_BYTES`. `max_attempts` defaults to `TASK_MAX_ATTEMPTS`.

#### `get_task`

```json
{"type": "object", "additionalProperties": false, "required": ["task_id"],
 "properties": {"task_id": {"$ref": "C/Uuid"}}}
```

Output: `{task: E/Task}`. Used by agents to re-read task state, for example after a `403 not_lease_owner`.

#### `get_pending_tasks`

```json
{"type": "object", "additionalProperties": false, "required": ["project"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "claimable_only": {"type": "boolean", "default": true},
   "limit": {"$ref": "C/Limit"},
   "cursor": {"$ref": "C/Cursor"}}}
```

Output: `{tasks: [E/TaskSummary], next_cursor: string|null}` (both required). With `claimable_only: true`, the same eligibility rules as `claim_task` apply to the caller. A caller without claim permission (a `reader`) receives an empty list; such callers should pass `claimable_only: false` to browse tasks.

#### `claim_task`

```json
{"type": "object", "additionalProperties": false, "required": ["project"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "task_id": {"$ref": "C/Uuid", "description": "Omit to take the next eligible task"},
   "instance_id": {"type": "string", "minLength": 1, "maxLength": 128,
                   "description": "Informational only; never used for authorization"}}}
```

Output: `{task: E/ClaimedTask | null}` (`task` required; null when nothing is claimable). The only response that ever contains `lease.lease_token`.

#### `update_task`

```json
{"type": "object", "additionalProperties": false, "required": ["task_id"],
 "properties": {
   "task_id": {"$ref": "C/Uuid"},
   "lease_token": {"$ref": "C/LeaseToken"},
   "status": {"enum": ["in_progress", "blocked", "pending", "failed", "cancelled"]},
   "progress_note": {"type": "string", "maxLength": 2000},
   "blocked_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
   "error_detail": {"type": "string", "minLength": 1, "maxLength": 4000},
   "retryable": {"type": "boolean", "default": true}},
 "allOf": [
   {"if": {"properties": {"status": {"const": "blocked"}}, "required": ["status"]},
    "then": {"required": ["blocked_reason"]}},
   {"if": {"properties": {"status": {"const": "failed"}}, "required": ["status"]},
    "then": {"required": ["error_detail"]}}]}
```

Output: `{task: E/Task, cancel_requested: boolean, idempotent_replay: boolean}` (all required). `lease_token` is required for every owner transition (`in_progress`, `blocked`, release to `pending`, `failed`, `cancelled`). Status `pending` has two meanings that the server distinguishes by task state: on a leased task it is a release by the owner and needs the token; on a `blocked` task it is an unblock by the task creator or a maintainer and presents no token. Terminal reports (`failed`, `cancelled`) follow Section 6.3.

#### `heartbeat_task`

```json
{"type": "object", "additionalProperties": false, "required": ["task_id", "lease_token"],
 "properties": {
   "task_id": {"$ref": "C/Uuid"},
   "lease_token": {"$ref": "C/LeaseToken"},
   "progress_note": {"type": "string", "maxLength": 2000}}}
```

Output: `{lease_expires_at: C/Timestamp, cancel_requested: boolean, max_runtime_deadline: C/Timestamp}` (all required).

#### `complete_task`

```json
{"type": "object", "additionalProperties": false,
 "required": ["task_id", "lease_token", "result_summary"],
 "properties": {
   "task_id": {"$ref": "C/Uuid"},
   "lease_token": {"$ref": "C/LeaseToken"},
   "result_summary": {"type": "string", "minLength": 1, "maxLength": 20000},
   "artifact_ids": {"type": "array", "items": {"$ref": "C/Uuid"}, "maxItems": 50, "uniqueItems": true}}}
```

Output: `{task: E/Task, idempotent_replay: boolean}` (both required). Replay and conflict behavior is specified in Section 6.3.

#### `publish_artifact`

```json
{"type": "object", "additionalProperties": false,
 "required": ["project", "artifact_type", "title", "content"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "artifact_type": {"$ref": "C/ArtifactType"},
   "title": {"type": "string", "minLength": 1, "maxLength": 200},
   "content": {"type": "string", "minLength": 1, "maxLength": 262144},
   "summary": {"type": "string", "maxLength": 1000},
   "document_key": {"$ref": "C/DocumentKey"},
   "task_id": {"$ref": "C/Uuid"},
   "lease_token": {"$ref": "C/LeaseToken"},
   "idempotency_key": {"$ref": "C/IdempotencyKey"}},
 "dependentRequired": {"task_id": ["lease_token"]}}
```

Output: `{artifact: E/Artifact, idempotent_replay: boolean}` (both required). The artifact is returned in status `draft`; publication is asynchronous (Section 7.2). `content` is limited to `MAX_ARTIFACT_BYTES`. When `task_id` is given, the task must be `claimed` or `in_progress` and the lease token must be current.

#### `get_artifact`

```json
{"type": "object", "additionalProperties": false, "required": ["artifact_id"],
 "properties": {
   "artifact_id": {"$ref": "C/Uuid"},
   "include_content": {"type": "boolean", "default": false}}}
```

Output: `{artifact: E/Artifact}`. `content` is returned only when requested and permitted by Section 8.2 visibility rules.

#### `search_knowledge`

```json
{"type": "object", "additionalProperties": false, "required": ["project", "query"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "query": {"type": "string", "minLength": 1, "maxLength": 500},
   "artifact_type": {"$ref": "C/ArtifactType"},
   "include_unpublished": {"type": "boolean", "default": false},
   "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}}
```

Output: `{results: [E/SearchResult]}` (required), best matches first. No cursor: results are bounded to the top `limit`. The query is treated as literal text (Section 5.5).

#### `list_project_artifacts`

```json
{"type": "object", "additionalProperties": false, "required": ["project"],
 "properties": {
   "project": {"$ref": "C/ProjectRef"},
   "status": {"$ref": "C/PublicationStatus"},
   "artifact_type": {"$ref": "C/ArtifactType"},
   "limit": {"$ref": "C/Limit"},
   "cursor": {"$ref": "C/Cursor"}}}
```

Output: `{artifacts: [E/ArtifactSummary], next_cursor: string|null}` (both required).

### 8.3.4 Permissions, idempotency, pagination and errors by tool

"Common errors" are those listed in Section 8.3.1. Permission names refer to the role matrix in Section 8.2.

| Tool | Permission | Idempotency | Pagination and ordering | Tool-specific errors (in addition to common) |
|---|---|---|---|---|
| `register_agent` | Any authenticated agent | Naturally idempotent (sets profile) | None | none |
| `get_agent_status` | Self; or another agent sharing a project the caller can read | Read | None | none |
| `list_agents` | reader | Read | None; at most 200 | none |
| `publish_message` | contributor | `idempotency_key`; unique per project and sender | None | `idempotency_conflict` |
| `get_messages` | reader | Read | Cursor; `created_at` ascending, then `id` | none |
| `mark_message_read` | reader (own receipt) | Naturally idempotent | None | none |
| `create_task` | contributor | `idempotency_key`; unique per project | None | `idempotency_conflict` |
| `get_task` | reader | Read | None | none |
| `get_pending_tasks` | reader | Read | Cursor; `priority` descending, `created_at` ascending, then `id` | none |
| `claim_task` | contributor | Not replayable (a lost response is recovered by lease expiry) | None | `task_not_claimable` (only when `task_id` is given) |
| `update_task` | Lease owner with token; task creator or maintainer for unblock | Terminal reports replay per Section 6.3 | None | `not_lease_owner`, `invalid_transition`, `completion_conflict` |
| `heartbeat_task` | Lease owner with token | Naturally idempotent | None | `not_lease_owner` |
| `complete_task` | Lease owner with token | Replay per Section 6.3 | None | `not_lease_owner`, `invalid_transition`, `completion_conflict` |
| `publish_artifact` | contributor; lease owner with token when `task_id` is given | `idempotency_key`; unique per project | None | `idempotency_conflict`, `artifact_open_version_exists`, `not_lease_owner`, `task_not_leased`, `content_policy_violation` |
| `get_artifact` | reader for published; own for contributor; all for maintainer | Read | None | none |
| `search_knowledge` | reader | Read | None; bounded by `limit` (maximum 20) | `validation_error` for an empty query |
| `list_project_artifacts` | reader (visibility per Section 8.2) | Read | Cursor; `created_at` descending, then `id` | none |

### 8.3.5 Generation and conformance

The schemas above are the contract, but they must not be maintained by hand in parallel with the code.

1. **Single source of truth.** Define one Pydantic model per tool input and output, and per shared entity, in `src/schemas/`. The REST API and the MCP tool handlers both use these models.
2. **Generated, committed schemas.** A script generates JSON Schema files from the models into `docs/mcp-schemas/` (one file per tool, plus `common.json` and `entities.json`), and generates `docs/mcp-tools.md` from the same models. Both are committed.
3. **Conformance test.** A contract test starts the server, calls `tools/list`, and fails if any advertised tool name, input schema or declared output schema differs from the committed files. Another test validates sample requests and responses for every tool against the schemas, including every error code listed in Section 8.3.4.
4. **Output schemas.** Declare output schemas and return `structuredContent` alongside the text content where the pinned MCP SDK and the target clients support it. The text content must always carry the same JSON so clients without structured-output support still work. Verify support in the installed Hermes, OpenCode and VS Code/Copilot versions rather than assuming it.
5. **Compatibility.** Within `/api/v1` and this tool set, changes are additive only: new optional input fields and new output fields. Removing or renaming a field, tightening a limit, or adding a required input field requires a new API version.
6. **Client documentation.** `docs/mcp-tools.md` is the reference used when configuring Hermes, OpenCode and Copilot. Client configuration examples must be written only after verifying the syntax against the installed versions.

## 8.4 Authentication and transport security for MCP and REST

- **Agent authentication:** `Authorization: Bearer ah_<prefix>_<secret>` on every request. Missing, invalid, revoked or expired tokens, and tokens of disabled agents, return `401`.
- The MCP endpoint does not implement an OAuth flow in the MVP. MCP clients must be configured with a static bearer header. Before writing client setup documentation, verify the exact configuration syntax in the installed Hermes, OpenCode and VS Code/Copilot versions, and keep tokens out of committed configuration files.
- **Origin and Host validation:** For Streamable HTTP, validate the `Origin` header on every MCP request to prevent DNS rebinding, and validate `Host` against `ALLOWED_HOSTS`. Requests with an `Origin` that is present but not in `ALLOWED_ORIGINS` are rejected with `403`. Requests with no `Origin` (non-browser clients) are accepted.
- **Stateless mode:** Configure the MCP SDK for stateless Streamable HTTP so that client sessions survive container restarts. Verify the option names against the pinned SDK version. Per-request authentication context must be passed to tool handlers explicitly (for example through a context variable set by middleware) and never stored in shared module state.
- **Browser sessions:** Opaque random session tokens in `HttpOnly`, `SameSite=Lax` cookies, with `Secure` according to the transport mode (Section 10.4). State-changing cookie-authenticated requests require an `X-CSRF-Token` header derived from the session's `csrf_secret`. Sessions have an idle timeout (default 12 hours) and an absolute timeout (default 7 days).

## 8.5 MCP resources

Expose read-only resources, all subject to the same authorization as tools:

| URI | Content |
|---|---|
| `agenthub://projects/{slug}` | Project overview and `instructions` |
| `agenthub://projects/{slug}/messages/recent` | Recent project messages |
| `agenthub://tasks/{id}` | Current task context |
| `agenthub://agents/{id}` | Agent status and capabilities |
| `agenthub://projects/{slug}/artifacts` | Published artifact index |

## 8.6 MCP prompts

Provide reusable prompts:

- `research_and_publish`
- `implement_from_research`
- `request_research`
- `review_implementation`

The prompts should establish consistent AgentHub workflows without replacing each agent's own system instructions. They must state that retrieved messages, tasks and artifacts are untrusted input.

## 8.7 Agent polling protocol

MCP does not guarantee that an inactive OpenCode or Copilot session will wake up when a task arrives. The universal mechanism is polling:

1. Call `claim_task` no more often than every 10 seconds while idle (the server rate-limits and may return `429`). Supply an `instance_id` that is unique to the process (for example a random UUID generated at start-up). Keep the returned `lease_token` in memory only, and never share it between instances.
2. While working, call `heartbeat_task` with the token every `TASK_HEARTBEAT_SECONDS` and honor `cancel_requested`.
3. Finish with `complete_task`, or report failure or blockage with `update_task`, always presenting the token. Publish task-linked artifacts with `publish_artifact` (`task_id` and `lease_token`) before completing, and list them in `artifact_ids`.

Retries after a lost response:

- It is safe to repeat, with identical arguments: `complete_task`, `update_task` with `failed` or `cancelled`, `heartbeat_task`, `mark_message_read`, and any create or publish call that carries the same `idempotency_key`. Replays return `idempotent_replay: true`.
- A repeat with different arguments returns `409 completion_conflict` (terminal reports) or `409 idempotency_conflict` (creates).
- On `403 not_lease_owner`, stop work and discard the result. The lease is no longer yours. Call `get_task` to see the current state rather than retrying blindly.
- A lost `claim_task` response cannot be recovered. Wait for the lease to expire; the task will be retried.

## 8.8 Events, SSE and polling

All event consumption reads `events_outbox` by `seq`.

**SSE endpoint:** `GET /api/v1/events/stream[?project=<slug>]`

- Authenticated by session cookie (dashboard) or bearer token (agents).
- Each event is sent with `id: <seq>`. Clients reconnect with `Last-Event-ID` to resume.
- If the requested cursor is older than the retained log, send a `resync` event so the client reloads its state.
- `app-core` runs a **single shared tailer** that reads new rows about once per second and fans them out to in-memory subscriber queues. It must not open one database query loop per connection and must not hold a read transaction between polls.
- Filter events by the subscriber's project access. Payloads contain identifiers and statuses only (Section 5.3).
- Send a comment heartbeat every 15 seconds, set `X-Accel-Buffering: no`, and cap concurrent streams (`SSE_MAX_CONNECTIONS`, default 20).

**Polling endpoint:** `GET /api/v1/events?after_seq=<n>&limit=<n>` returns events with `seq > n` that the caller may see.

Agents wake-up: the outbox dispatcher can send wake-up hints to adapter-mode agents (Section 9). Poll-mode agents rely on the polling protocol in 8.7.

---

# 9. Agent adapters and execution

Keep the following components separate:

| Component | Responsibility |
|---|---|
| MCP server | Exposes AgentHub tools, resources, and prompts |
| Task engine | Owns task state, claims, leases, retries, and completion |
| Agent adapter | Converts AgentHub tasks into the agent's execution mechanism |
| Artifact service | Publishes and retrieves GitHub knowledge |
| Notification service | Delivers events and supports polling |

## 9.1 Hermes

Hermes is the first autonomous integration. Integrate it in two stages.

**Stage 1 (baseline, required): polling MCP client.** Hermes runs with `execution_mode = 'poll'`. It follows the polling protocol in Section 8.7: claim, heartbeat, publish artifacts and complete, all through MCP. This requires no AgentHub-to-Hermes connectivity and is the integration the definition of done depends on.

**Stage 2 (optional): adapter wake-up.** After Stage 1 works, an adapter may reduce latency by notifying Hermes that work is available.

Before implementing either stage:

- Inspect the installed Hermes version.
- Verify its actual background execution mechanism, how it receives tasks and reports results, and how it can call AgentHub MCP tools.
- Do not assume command names or API behavior that have not been confirmed.
- Record the verified mechanism in `docs/hermes-integration.md`.

If Hermes cannot run its own scheduled loop that calls MCP, provide a minimal reference script, `scripts/hermes_poll_worker.py`, that acts as an MCP client running the polling protocol and invokes Hermes through its verified interface.

## 9.1.1 Adapter contract

Adapters live in `src/adapters/` and implement:

```python
class AgentAdapter(Protocol):
    async def health(self, agent: AgentRecord) -> AdapterHealth: ...
    async def dispatch(self, agent: AgentRecord,
                       task: TaskSummary) -> DispatchResult: ...
```

Rules:

1. **`dispatch` is a wake-up hint only.** The adapter never claims tasks on behalf of an agent. The agent still claims through MCP, which keeps one claim path with one set of authorization rules.
2. `dispatch` must be idempotent, because outbox delivery is at-least-once.
3. Enforce a timeout (default 30 seconds) and a per-agent concurrency of 1.
4. Allowed transports are an HTTP(S) endpoint on the LAN, or a fixed command from a deployment-time allowlist. **Never** build a command line from task content, project content or database-supplied strings, and never mount the Docker socket into the worker. Task content is data and must be passed as data.
5. `adapter_config` must not contain secrets. Reference secret files by name.
6. Adapter failures are recorded on the outbox event and must not affect task state.

## 9.2 OpenCode

Initially integrate OpenCode as an MCP-connected client.

Add background execution only after validating a supported execution mechanism.

## 9.3 VS Code/GitHub Copilot

Initially integrate Copilot through its MCP support in VS Code.

Do not assume that an inactive Copilot session can be woken by AgentHub.

---
# 10. Docker Compose deployment

## 10.1 Compose configuration

Illustrative starting point. Validate it against the installed Docker Compose version. There is intentionally no top-level `version` key.

```yaml
name: agenthub

x-backend: &backend
  build:
    context: .
    dockerfile: deploy/Dockerfile.backend
  image: agenthub-backend:local
  user: "10001:10001"
  read_only: true
  tmpfs:
    - /tmp
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
  restart: unless-stopped
  logging:
    driver: json-file
    options:
      max-size: "10m"
      max-file: "3"
  env_file:
    - .env

services:
  app-core:
    <<: *backend
    command: ["/app/deploy/entrypoint-app.sh"]
    volumes:
      - sqlite_data:/app/data
    secrets:
      - app_secret_key
      - github_webhook_secret
    ports:
      - "127.0.0.1:8000:8000"      # health and debugging only
    mem_limit: 512m
    memswap_limit: 512m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s

  worker:
    <<: *backend
    command: ["python", "-m", "src.workers.main"]
    depends_on:
      app-core:
        condition: service_healthy
    volumes:
      - sqlite_data:/app/data
      - ${BACKUP_DIR:-./backups}:/app/backups
    secrets:
      - app_secret_key
      - github_app_private_key
    mem_limit: 384m
    memswap_limit: 384m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/worker_heartbeat')<60 else 1)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s

  web:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    depends_on:
      app-core:
        condition: service_healthy
    ports:
      - "${LAN_BIND_IP}:${WEB_PORT:-3000}:80"
    mem_limit: 128m
    memswap_limit: 128m
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  sqlite_data:

secrets:
  app_secret_key:
    file: ./secrets/app_secret_key.txt
  github_app_private_key:
    file: ./secrets/github_app_private_key.pem
  github_webhook_secret:
    file: ./secrets/github_webhook_secret.txt
```

Implementation notes:

- The backend image must create `/app/data` owned by UID 10001 so the named volume is writable by the non-root user, and must include everything the health checks and entrypoint need. The health checks use Python, so no `curl` is required.
- If TLS is terminated in the `web` container (Section 10.4), publish 443 as well and mount the certificate and key as read-only secrets into `web` only.
- Create secret files with restrictive permissions and exclude `secrets/` from version control. The GitHub private key is mounted only into `worker`. The frontend container never receives backend secrets.
- Do not publish `app-core` on a LAN address. All external access goes through `web`.

## 10.2 Nginx requirements

The `web` container serves the static dashboard and reverse-proxies `/api/v1` and `/mcp`. SSE and MCP streaming require buffering to be disabled:

```nginx
location /api/v1/ {
    proxy_pass http://app-core:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    client_max_body_size 1m;
}

location /mcp {
    proxy_pass http://app-core:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    client_max_body_size 1m;
}
```

Also configure: the SPA fallback to `index.html`, long-lived caching for hashed static assets, no caching of `/api/v1` responses, and standard security headers (`X-Content-Type-Options`, `Referrer-Policy`, a restrictive `Content-Security-Policy`). Browser requests are same-origin through Nginx, so CORS should be disabled by default; if a separate origin is ever needed, add it explicitly to `ALLOWED_ORIGINS`.

## 10.3 Docker Compose resource limits

| Container | Hard memory limit |
|---|---:|
| app-core | 512 MB |
| worker | 384 MB |
| web | 128 MB |
| **Total** | **1,024 MB (1.0 GB)** |

The target is a measured typical working set below 800 MB across all three containers, leaving the remainder of the Pi's 4 GB for the operating system and for any agents that run on the same machine. These are initial limits, not guaranteed values.

Verification on the target Pi:

1. Run `docker info` and confirm there is no warning that the kernel does not support memory limits. If there is, enable memory cgroups by adding `cgroup_enable=memory cgroup_memory=1` to the kernel command line (`/boot/firmware/cmdline.txt` on current Raspberry Pi OS) and reboot.
2. After `docker compose up -d`, confirm enforcement with `docker inspect --format '{{.HostConfig.Memory}}' <container>` for each container.
3. Measure peak usage with `docker stats --no-stream` during the representative workload in Section 16.
4. Confirm that an OOM-killed container restarts and recovers cleanly.

## 10.4 Network binding and transport security

`LAN_BIND_IP` must contain the Pi's reserved LAN address (for example `192.168.1.10`). Binding Uvicorn to `0.0.0.0` inside a container is normal, but host-published ports must be restricted to the LAN interface or loopback, as in the Compose file.

Choose one transport mode and set it explicitly:

**Mode A: TLS (recommended).** Terminate TLS in `web`, using a certificate from Tailscale (`tailscale cert`), a local CA such as `mkcert`, or a reverse proxy such as Caddy. Set `PUBLIC_BASE_URL` to the `https://` URL and `COOKIE_SECURE=true`. Point MCP clients at the `https://` URL.

**Mode B: plain HTTP on a trusted LAN.** Set `COOKIE_SECURE=false` and `PUBLIC_BASE_URL` to the `http://` URL. Bearer tokens and session cookies then cross the LAN unencrypted. `app-core` must log a prominent startup warning in this mode. Use only on a network you control, and use a VPN for any remote access.

Never expose the Pi directly to the public internet.

---

# 11. Configuration

Example `.env.example`. Every variable must be documented in `docs/configuration.md` with its default, valid range and purpose.

```dotenv
APP_ENV=production
LOG_LEVEL=info

# Transport and network
LAN_BIND_IP=192.168.1.10
WEB_PORT=3000
PUBLIC_BASE_URL=http://192.168.1.10:3000
COOKIE_SECURE=false
ALLOWED_HOSTS=192.168.1.10:3000
ALLOWED_ORIGINS=http://192.168.1.10:3000

# Secrets (files mounted by Docker secrets)
APP_SECRET_KEY_FILE=/run/secrets/app_secret_key
GITHUB_PRIVATE_KEY_FILE=/run/secrets/github_app_private_key
GITHUB_WEBHOOK_SECRET_FILE=/run/secrets/github_webhook_secret

# Database
DATABASE_URL=sqlite+aiosqlite:////app/data/agenthub.db
ALLOW_UNSAFE_DB_FS=false

# GitHub
GITHUB_APP_ID=
GITHUB_INSTALLATION_ID=
GITHUB_REPOSITORY_OWNER=
GITHUB_KNOWLEDGE_REPOSITORY=agenthub-knowledge
GITHUB_DEFAULT_BRANCH=main
GITHUB_PR_POLL_INTERVAL_SECONDS=300
GITHUB_WEBHOOKS_ENABLED=false
PUBLISH_MAX_ATTEMPTS=5

# Tasks and leases
TASK_LEASE_SECONDS=120
TASK_HEARTBEAT_SECONDS=40
TASK_MAX_ATTEMPTS=3
TASK_RETRY_BACKOFF_SECONDS=30
TASK_RETRY_BACKOFF_MAX_SECONDS=600
TASK_MAX_RUNTIME_SECONDS=14400

# Worker and outbox
WORKER_POLL_INTERVAL_SECONDS=10
OUTBOX_POLL_INTERVAL_SECONDS=5
OUTBOX_PROCESSING_TIMEOUT_SECONDS=120
OUTBOX_MAX_ATTEMPTS=8
SSE_MAX_CONNECTIONS=20

# Limits
MAX_REQUEST_BYTES=1048576
MAX_TASK_PAYLOAD_BYTES=65536
MAX_MESSAGE_BYTES=65536
MAX_ARTIFACT_BYTES=262144
RATE_LIMIT_AGENT_PER_MINUTE=120
RATE_LIMIT_ANON_PER_MINUTE=600

# Sessions
SESSION_IDLE_TIMEOUT_SECONDS=43200
SESSION_ABSOLUTE_TIMEOUT_SECONDS=604800

# Retention (days)
EVENT_RETENTION_DAYS=30
FAILED_EVENT_RETENTION_DAYS=90
TASK_EVENT_RETENTION_DAYS=180
AUDIT_RETENTION_DAYS=365
GITHUB_DELIVERY_RETENTION_DAYS=30

# Backups
BACKUP_DIR=./backups
BACKUP_TIME_UTC=03:15
BACKUP_RETENTION_DAYS=14
```

Initial administrator: do not ship a default admin or password. Provision the first administrator with the one-time CLI command below, which prompts for the password interactively or reads it from a file.

```bash
docker compose run --rm app-core python -m src.cli create-admin --email you@example.com
```

## 11.1 Operational CLI

Provide and document these commands as `python -m src.cli <command>`, runnable with `docker compose run --rm` or `docker compose exec`:

| Command | Purpose |
|---|---|
| `create-admin` | One-time creation of an administrator |
| `create-agent`, `rotate-token`, `revoke-token`, `disable-agent` | Agent and token provisioning; raw tokens are printed once |
| `create-project`, `add-member` | Project and membership setup |
| `backup`, `restore` | WAL-safe backup and verified restore (Section 12.2) |
| `check-db` | Run integrity check, PRAGMA verification and startup checks |
| `reindex [--refresh]` | Rebuild FTS5; with `--refresh`, re-fetch published content from GitHub |

---

# 12. Reliability, security, and operations

## 12.1 Outbox and notification delivery

The event log and outbox rules are defined in Sections 5.3, 6.4 and 8.8. In summary:

- Events are written in the same transaction as the state change they describe.
- Active delivery (adapter wake-up hints) is at-least-once with a processing timeout, bounded backoff and a retry limit.
- Consumers deduplicate using the event ID.
- Permanently failed events are exposed in the dashboard.

## 12.2 Database backup and restore

Use SQLite's online backup API or an equivalent WAL-aware consistent backup procedure.

Do not copy only the active `.db` file while WAL mode is in use. Committed data may still be represented in the WAL file.

Requirements:

- Back up AgentHub operational data independently of GitHub knowledge.
- Implement `python -m src.cli backup` using Python's `sqlite3.Connection.backup()`, because the `sqlite3` command-line tool may not exist in the image. The command writes a timestamped file to `BACKUP_DIR`, runs `PRAGMA integrity_check` on the copy, and compresses it.
- The worker's maintenance loop runs the backup daily at `BACKUP_TIME_UTC` and keeps `BACKUP_RETENTION_DAYS` (default 14) daily copies.
- Store backups off the Pi where practical. Synchronize `BACKUP_DIR` from the host with a scheduled `rsync` or `rclone` job. Backups contain hashed tokens, messages and staged content, so encrypt copies that leave the device (for example with `age`).
- Document and test the restore procedure before declaring deployment complete.

Restore procedure:

1. Stop the stack: `docker compose stop web worker app-core`.
2. Move the current database, `-wal` and `-shm` files aside; do not delete them until the restore is verified.
3. Run `python -m src.cli restore <backup-file>`. It decompresses the file, runs `PRAGMA integrity_check`, checks that the schema version is not newer than the code, installs the file as the database and removes stale `-wal` and `-shm` files.
4. Start the stack and run `python -m src.cli check-db`.
5. Let the GitHub reconciler run, then run `reindex`, so artifact state and the FTS index heal against GitHub.

## 12.3 Security

- Store only hashed agent API tokens (Section 5.3). Display raw tokens only once.
- Enforce project membership and role checks on every REST and MCP operation through the shared service layer.
- Hash passwords using Argon2id. Lock accounts after repeated failed logins.
- Use `HttpOnly`, `SameSite` cookies for browser sessions, with `Secure` according to the transport mode (Section 10.4).
- Apply CSRF protection to state-changing cookie-authenticated requests.
- Validate `Origin` and `Host` on MCP requests (Section 8.4).
- Apply request size limits and rate limits (Section 8.1).
- Keep secrets out of images, source control, ordinary environment variables, and the frontend container. Mount secrets as files.
- Never log agent tokens, lease tokens, passwords, session identifiers, secret file contents, message bodies, task payloads or artifact content. Redact `Authorization` headers.
- Validate GitHub webhook signatures if webhooks are enabled.
- Apply the artifact content policy and path rules in Section 7.5.
- Audit token lifecycle, permission changes, task transitions, rejected stale-lease attempts, artifact publication, reviewer-edit overrides, login events and administrative actions.
- Treat research, repository files, messages, and artifacts as untrusted input.
- Retrieved content must not override agent system instructions or project security policy. Never execute commands built from task or artifact content (Section 9.1.1).
- Run containers as non-root with dropped capabilities, as in Section 10.1.

Do not expose the Pi directly to the public internet. Use a VPN such as WireGuard or Tailscale for remote access.

## 12.4 Raspberry Pi operating requirements

### Storage and startup checks

- SQLite database and WAL files must reside on a local Docker volume. Do not use network filesystems for SQLite.
- Prefer a reliable USB SSD over an SD card.
- At startup `app-core` must verify and refuse to start (with a clear error) if any check fails:
  1. The database directory is not on a network or FUSE filesystem (inspect `/proc/mounts` for types such as `nfs`, `cifs`, `smb3` and `fuse.sshfs`), unless `ALLOW_UNSAFE_DB_FS=true`.
  2. SQLite is version 3.35 or later, and FTS5 and JSON1 are available.
  3. `PRAGMA journal_mode` returns `wal`.
  4. The Alembic schema version matches the code.
  5. In Mode B (plain HTTP), log the unencrypted-transport warning.

### Maintenance and retention

The worker's maintenance loop must prune in small batches (at most 500 rows per transaction, with short pauses) so pruning never delays task claims:

| Data | Retention |
|---|---|
| `events_outbox` (`delivered`, `not_required`) | `EVENT_RETENTION_DAYS` (default 30) |
| `events_outbox` (`failed`) | `FAILED_EVENT_RETENTION_DAYS` (default 90) |
| `task_events` for terminal tasks | `TASK_EVENT_RETENTION_DAYS` (default 180) |
| `audit_logs` | `AUDIT_RETENTION_DAYS` (default 365) |
| Expired or revoked `sessions` | 7 days after expiry |
| `github_deliveries` | `GITHUB_DELIVERY_RETENTION_DAYS` (default 30) |
| `tasks`, `messages`, `artifacts` | Not pruned in the MVP |

SQLite housekeeping:

- Leave `wal_autocheckpoint` at its default and additionally run `PRAGMA wal_checkpoint(PASSIVE)` periodically. Run `PRAGMA wal_checkpoint(TRUNCATE)` after backups when idle. Report WAL size in `GET /health/details`.
- Run `PRAGMA optimize` daily.
- Do not run `VACUUM` automatically.

### Other requirements

- Keep database write transactions short.
- Verify memory limits are enforced (Section 10.3).
- Container logs are size-limited (Section 10.1) to protect flash storage.

---

# 13. Web dashboard

Build a lightweight React/TypeScript dashboard.

Required pages:

| Page | Function |
|---|---|
| Overview | Agent status, pending tasks, recent activity |
| Agents | Registration, capabilities, online status |
| Tasks | Task queue, ownership, leases, progress, retries |
| Messages | Agent-to-agent and project messages |
| Knowledge | Published research, specifications, decisions |
| Artifacts | PR status, GitHub links, publication history |
| Settings | Projects, permissions, system configuration |

Use SSE for live dashboard updates, with polling fallback.

The dashboard must display:

- Pending and active tasks.
- Task owner and lease expiry.
- Failed and blocked tasks.
- Agent last-seen time.
- Artifact publication status.
- GitHub PR links.
- Outbox delivery failures.
- Recent security/audit events.

Dashboard authentication and controls:

- Login uses the browser session flow in Section 8.4. The SPA sends the CSRF token on every state-changing request.
- The Settings page is visible to admins only for user, agent, token, project and membership management. Raw agent tokens are shown once at creation or rotation.
- Project maintainers and admins can cancel tasks and retry failed tasks from the Tasks page. Dashboard users cannot create tasks, messages or artifacts.
- Live updates use the SSE endpoint (Section 8.8), with automatic fallback to polling `GET /events?after_seq=`. On a `resync` event the SPA reloads its data.
- The dashboard also displays worker health (heartbeat age), outbox queue depth, and WAL size from `GET /health/details`.
- Build the frontend off the Pi (CI or `docker buildx` for `linux/arm64`) and ship the built image or static assets. A Node build on a 4 GB Pi can exhaust memory.

---

# 14. Implementation phases

Implement in phases. Each phase must be independently testable. Build a thin end-to-end slice early so that the core workflow is exercised before GitHub and the dashboard exist.

## Phase 1 — Pi foundation

- Set up repository structure.
- Create Docker Compose configuration with memory limits, non-root users and log limits.
- Build the consolidated FastAPI application and the worker skeleton with its heartbeat file.
- Configure SQLite, WAL mode, `write_tx()` and migrations run by `app-core`.
- Implement startup checks and health checks.
- Verify clean deployment, restart and memory-limit enforcement on the Pi.

## Phase 2 — Identity and permissions

- Implement users, sessions, CSRF, login lockout and rate limiting.
- Implement agents, token generation and bearer-token authentication.
- Implement projects, agent membership, user project access and the role matrix.
- Implement the operational CLI commands for provisioning.
- Add audit logging.

## Phase 3 — Messaging and task engine

- Implement messages and lazy receipts.
- Implement task creation and idempotency.
- Implement atomic task claiming, the full state machine, leases with fencing tokens, heartbeats, terminal-report idempotency, cancellation and blocked handling.
- Implement the lease reaper, retries and backoff.
- Implement the event log, outbox dispatcher and recovery of stuck events.
- Implement REST endpoints and the SSE/polling event feed.

## Phase 4 — MCP interface and first vertical slice

- Implement Streamable HTTP MCP tools, resources and prompts as thin adapters over the service layer.
- Define the Pydantic input and output models in `src/schemas/`, generate the JSON Schemas and `docs/mcp-tools.md`, and add the contract conformance test (Section 8.3.5).
- Implement `Origin`/`Host` validation and stateless mode.
- Verify project authorization and the role matrix across both REST and MCP.
- Build a small reference polling client (`scripts/poll_client.py`) and demonstrate the full loop: create task, claim, heartbeat, complete.
- Verify Hermes and OpenCode connectivity.

## Phase 5 — GitHub artifact service

- Configure GitHub App (or PAT) authentication and repository protection.
- Implement content policy, versioning and path derivation.
- Implement the idempotent artifact publisher, branch and pull request creation.
- Implement the polling reconciler, merge-commit verification and reviewer-edit handling.
- Implement optional webhook verification.
- Implement FTS5 indexing, search, `reindex`.
- Verify that published artifacts become searchable with the correct commit SHA.

## Phase 6 — Agent execution

- Complete the Hermes Stage 1 polling integration and document it in `docs/hermes-integration.md`.
- Verify task dispatch, heartbeat, artifact publication and completion with Hermes.
- Optionally implement the Hermes adapter wake-up hint (Stage 2).
- Add OpenCode integration after validating its execution interface.
- Keep Copilot as an MCP-connected client initially.

## Phase 7 — Dashboard and hardening

- Build the React dashboard off the Pi.
- Add the backup and restore commands, retention jobs and scheduled backups.
- Add monitoring and error reporting.
- Test security and authorization.
- Measure actual resource usage on the Pi.
- Run end-to-end workflow tests.

---

# 15. Definition of done

The MVP is complete when:

1. AgentHub deploys using `docker compose up -d` on Raspberry Pi OS 64-bit.
2. The database persists across container restarts.
3. Container memory limits (512 MB, 384 MB and 128 MB) are verified as enforced, and the stack's measured typical working set is below 800 MB under the representative workload.
4. Hermes, as a polling MCP client, can receive tasks, heartbeat, publish research artifacts, and complete work.
5. OpenCode can retrieve tasks and published research over MCP.
6. Copilot can connect to the MCP endpoint and retrieve published knowledge.
7. Task claims are atomic and cannot be duplicated, including across `app-core` and `worker` processes.
8. Expired leases are recovered correctly, and an unassigned task whose lease expired can be claimed by a different agent.
9. Lease operations are fenced by lease token: a stale execution instance sharing an agent's credentials cannot heartbeat, update, complete or publish against a lease it no longer holds.
10. Terminal task reports are idempotent: a repeated identical completion returns the existing result, and a conflicting repeat returns `409`.
11. Agent permissions, including the role matrix in Section 8.2, are enforced identically over REST and MCP.
12. Every MCP tool's advertised schema matches the committed JSON Schemas in `docs/mcp-schemas/`, verified by the contract test.
13. Outbox delivery survives worker restarts, and stuck `processing` events are recovered.
14. Artifacts transition to `published` only after verified GitHub pull request merge, and `commit_sha` is the merge commit.
15. Polling reconciles pull request status when webhooks are unavailable.
16. FTS5 returns the correct artifact references and commit SHA, scoped to the caller's projects.
17. Database backup and restore procedures have been tested on a live WAL-mode database.
18. The dashboard displays agents, tasks, messages, artifacts, publication status, outbox failures and worker health, and updates live through SSE behind Nginx.
19. The transport mode (TLS or plain HTTP LAN) is configured explicitly and documented.

---

# 16. Additional acceptance tests

The following tests must pass. Tests that involve concurrency must use separate OS processes, not only coroutines.

**Task engine**

1. Two agents in separate processes repeatedly attempting to claim the same pending task (at least 200 iterations) never both succeed.
2. An agent outside a project cannot read its messages, claim its tasks, receive its events or search its artifacts.
3. An agent cannot heartbeat, update, or complete a task leased to another agent, including after the original lease was reaped and re-claimed.
4. A task with a non-null idempotency key cannot be duplicated within its project; an identical replay returns the existing task and a different payload returns `409 idempotency_conflict`.
5. Expired leases are recovered correctly: back to `pending` with backoff, to `failed` when attempts are exhausted, and to `cancelled` when cancellation was requested.
6. **Regression:** an unassigned task whose first claimer disappears becomes claimable by a different eligible agent after lease expiry.
7. Table-driven tests of the role matrix pass identically through REST and MCP, including `reader` being unable to claim, create, or publish.
8. Invalid state transitions are rejected with `409 invalid_transition`, and the lease-consistency `CHECK` constraints reject inconsistent rows (owner, expiry and token hash set or cleared together; `final_*` fields set or cleared together).

**Lease fencing**

9. **Same-identity stale instance.** Instance A claims a task and its lease expires; the reaper returns the task to `pending`; instance B, using the same agent credentials, claims it. Instance A's `heartbeat_task`, `update_task`, `complete_task` and task-linked `publish_artifact` are all rejected with `403 not_lease_owner`, while instance B's succeed and B's result is the one recorded.
10. Two instances sharing one agent identity racing to claim the same task, repeated across separate processes, never both obtain a valid lease token.
11. A valid token with the wrong agent ID, and a correct agent ID with a missing or wrong token, are both rejected. Error responses do not reveal the lease owner or the task result.
12. An owner holding a valid token succeeds while its lease is expired but not yet reaped, and is rejected once the task has been reaped and re-claimed.
13. The database never contains a raw lease token, and lease tokens do not appear in logs, audit entries or task events (only the short fingerprint).
14. Rejected stale-writer attempts are recorded as `task.stale_lease_rejected`, rate-limited to once per minute per task and agent.
15. `instance_id` has no effect on authorization: two instances with identical `instance_id` values and different tokens are still distinguished.

**Terminal report idempotency**

16. Repeating an identical `complete_task` after the first succeeded returns `200`, the existing result and `idempotent_replay: true`, and writes no additional `task_events`, outbox events or audit entries.
17. Repeating `complete_task` with a different `result_summary` or different `artifact_ids` returns `409 completion_conflict`, and the stored result is unchanged.
18. A `complete_task` from a stale lease generation returns `403 not_lease_owner`, including after another instance has completed the task.
19. `complete_task` after a recorded failure by the same token returns `409 invalid_transition`; the same identical `update_task` failure or cancellation report repeated returns `idempotent_replay: true`.
20. The replay still works after retention pruning has removed the task's `task_events`.
21. A maintainer retry of a failed task clears the `final_*` fields; the previous owner's token can no longer replay or conflict.
22. `complete_task` with `artifact_ids` from another project, or linked to another task, is rejected with `422 validation_error` and applies nothing.
23. Non-terminal owner reports (release, blocked, retryable failure) repeated after success return `403 not_lease_owner`, and `get_task` returns the current state.

**Message and artifact idempotency**

24. Repeating `publish_message` or `publish_artifact` with the same `idempotency_key` and identical request returns the existing object with `idempotent_replay: true`; a different request returns `409 idempotency_conflict`. A retried `publish_artifact` never returns `artifact_open_version_exists`.
25. `publish_artifact` with a `task_id` is rejected with `403 not_lease_owner` for a stale token and `409 task_not_leased` when the task is not `claimed` or `in_progress`.

**MCP contract**

26. The contract test fails when a tool is added, removed or changed without regenerating `docs/mcp-schemas/`.
27. For every tool, valid sample requests and responses validate against the committed schemas, and every error code in Section 8.3.4 is produced by at least one test.
28. Requests with unknown fields, wrong types, out-of-range limits, or a `task_id` without a `lease_token` on `publish_artifact` are rejected with `422 validation_error`.
29. Cursor pagination returns every item exactly once under concurrent inserts, and a cursor reused with different filters is rejected.

**Events**

30. A worker killed mid-delivery leaves no lost events; stuck `processing` rows are recovered after the timeout and consumers dedupe by event ID.
31. Events produced by the worker appear on `app-core` SSE streams, resume correctly with `Last-Event-ID`, are filtered by project access, and stream without buffering through Nginx.

**GitHub and artifacts**

32. A missed GitHub webhook (or webhooks disabled) is corrected by periodic polling.
33. Invalid webhook signatures and duplicate delivery IDs are rejected or safely ignored.
34. Killing the worker after the branch is created and before the database update does not produce duplicate branches or pull requests on retry.
35. A pull request merged with reviewer edits results in `content` updated from GitHub, a recorded audit entry, and `commit_sha` equal to the merge commit.
36. Publishing a second version supersedes the first and removes the old FTS row only after the new version is merged; publishing while a version is open fails with `409 artifact_open_version_exists`.
37. Malicious `document_key` or path inputs (`../`, absolute paths, separators, encoded and Unicode tricks), spoofed front matter, secret-pattern content and oversized content are rejected.
38. FTS5 indexes published artifacts and returns the artifact UUID and source commit SHA. FTS removes stale or rejected entries. Queries containing quotes, `*`, `OR`, `NEAR(` or column filters do not error or cross project boundaries.

**Database and operations**

39. A live WAL-mode database under write load can be backed up and restored using the documented procedure, and the restored copy passes `PRAGMA integrity_check`.
40. A sustained mixed write load from `app-core` and `worker` for five minutes produces no unhandled `SQLITE_BUSY` errors.
41. Retention pruning runs without increasing claim latency beyond an agreed bound (for example p95 below 250 ms).
42. Startup refuses to run when the database is on a network filesystem (simulated), or FTS5/JSON1 is unavailable, and `app-core` alone runs migrations while the worker waits.
43. The database survives container restart, and the worker health check fails when the worker stalls.

**Security**

44. MCP requests with a disallowed `Origin` are rejected; missing, invalid, revoked, expired tokens and tokens of disabled agents return `401`; agent tokens never appear in logs.
45. Login lockout, CSRF enforcement, session expiry and cookie flags behave according to `COOKIE_SECURE`.

**Resources**

46. The deployed Compose stack's configured memory limits are verified with `docker inspect`, and actual usage is measured with `docker stats` under a representative workload, including recovery after an OOM kill.

---

# 17. Suggested source tree

```text
agenthub/
├── src/
│   ├── main.py
│   ├── cli.py
│   ├── config.py
│   ├── database/
│   │   ├── connection.py          # PRAGMAs, write_tx()
│   │   ├── types.py               # canonical timestamp TypeDecorator
│   │   ├── models.py
│   │   ├── migrations/
│   │   └── repositories/
│   ├── api/
│   │   ├── routes/
│   │   └── dependencies.py
│   ├── schemas/                   # Pydantic models: single source for REST and MCP contracts
│   ├── mcp/
│   │   ├── server.py
│   │   ├── tools/
│   │   ├── resources/
│   │   └── prompts/
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── task_service.py
│   │   ├── message_service.py
│   │   ├── artifact_service.py
│   │   ├── github_service.py
│   │   ├── search_service.py
│   │   ├── event_service.py
│   │   └── maintenance_service.py
│   ├── workers/
│   │   ├── main.py
│   │   ├── lease_reaper.py
│   │   ├── outbox_dispatcher.py
│   │   ├── artifact_publisher.py
│   │   ├── github_reconciler.py
│   │   └── maintenance.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── hermes.py
│   │   ├── opencode.py
│   │   └── copilot.py
│   └── security/
│       ├── authentication.py
│       ├── authorization.py
│       ├── tokens.py
│       ├── sessions.py
│       ├── rate_limit.py
│       └── content_policy.py
├── frontend/
│   ├── src/
│   ├── Dockerfile
│   └── package.json
├── deploy/
│   ├── Dockerfile.backend
│   ├── entrypoint-app.sh
│   └── nginx.conf
├── scripts/
│   ├── poll_client.py
│   └── hermes_poll_worker.py
├── docs/
│   ├── configuration.md
│   ├── operations.md
│   ├── mcp-tools.md               # generated from the Pydantic models
│   ├── mcp-schemas/               # generated JSON Schemas (committed)
│   └── hermes-integration.md
├── secrets/
│   └── .gitkeep
├── backups/
│   └── .gitkeep
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── concurrency/
│   ├── security/
│   ├── contract/                  # MCP schema conformance
│   └── e2e/
├── compose.yaml
├── alembic.ini
├── .env.example
├── .gitignore                     # must exclude secrets/*, backups/*, .env
└── README.md
```

---

# 18. Implementation guidance for coding agents

The coding agent implementing AgentHub must follow these requirements:

1. Implement the project in phases rather than one large change.
2. Keep each phase independently testable.
3. Use Alembic migrations as the only schema source; migrations run only in `app-core`.
4. Keep SQLite-specific transaction and FTS5 code behind repository interfaces. All writes go through `write_tx()` (`BEGIN IMMEDIATE`).
5. Do not introduce PostgreSQL, Redis, or a vector database for the MVP.
6. Do not execute network calls inside SQLite write transactions.
7. Implement bounded retry for SQLite lock acquisition only.
8. Implement REST and MCP as thin adapters over one service layer. Enforce project membership, role and task ownership only in that layer.
9. Make event delivery and GitHub reconciliation idempotent.
10. Use the canonical timestamp format everywhere; never compare it with `CURRENT_TIMESTAMP` output.
11. Never modify `assigned_agent_id` during a claim or lease recovery.
12. Include unit, integration and multi-process concurrency tests for task claims, leases, retries, permissions, events, FTS5, publication crash-safety and backup/restore (Section 16).
13. Do not assume an MCP client can wake an inactive agent. Poll-mode is the baseline.
14. Before implementing the Hermes integration, inspect the installed Hermes version and verify its supported background execution mechanism. Record findings in `docs/hermes-integration.md`.
15. Treat OpenCode and Copilot as MCP clients until a separately validated background adapter exists.
16. Adapters send wake-up hints only; never build commands from task content and never mount the Docker socket.
17. Pin dependency versions (including the MCP SDK) and verify that all Python wheels are available for `aarch64`.
18. Verify MCP SDK option names and client header configuration against the installed versions rather than assuming them.
19. Keep secrets out of source control, Docker images, logs and frontend assets.
20. Document every configuration variable and every operational command.
21. Never authorize a lease-scoped operation by agent ID alone. Always verify the lease token hash as well (Section 6.3). Store only the hash and never log the token.
22. Make terminal owner reports idempotent using the `final_*` columns, and implement the decision order in Section 6.3 exactly.
23. Define the Pydantic models first and generate the MCP and REST schemas from them; do not hand-maintain the JSON Schemas (Section 8.3.5).
24. Do not use `instance_id` for any authorization decision.

**Final architectural principle:** AgentHub is a lightweight coordination and knowledge service, not an AI inference platform. SQLite manages operational state, GitHub manages durable knowledge, MCP provides the agent interface, and adapters manage actual agent execution. This separation should be preserved throughout implementation.
