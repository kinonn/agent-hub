# Configuration reference

All variables are read by both backend processes from `.env`. Secret values are mounted as files under `/run/secrets`; do not place them in `.env`.

| Variable | Default | Range / format | Purpose |
|---|---|---|---|
| `APP_ENV` | `production` | `production`, `development`, `test` | Controls production startup checks and hides API docs in production. |
| `LOG_LEVEL` | `info` | Python logging level | Log verbosity; request bodies and credentials are never logged. |
| `LAN_BIND_IP` | required | Reserved IPv4/IPv6 LAN address | Host interface for the Nginx published port. Do not use `0.0.0.0`. |
| `WEB_PORT` | `3000` | 1–65535 | Host port published by Nginx. |
| `WEB_IMAGE` | `agenthub-web:local` | Docker image reference | Prebuilt ARM64 dashboard image; build it off the Pi and load or pull it before `docker compose up`. |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:3000` | `http://` or `https://` URL | Public dashboard/MCP base URL for client setup and deployment documentation. |
| `COOKIE_SECURE` | `false` | boolean | Set `true` when TLS terminates before the browser; controls the session cookie `Secure` flag. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` | Comma-separated exact host values, including port when supplied | Allowed `Host` values for MCP requests. |
| `ALLOWED_ORIGINS` | empty | Comma-separated exact origins | Browser `Origin` allowlist for MCP. Requests without an `Origin` are accepted. |
| `APP_SECRET_KEY_FILE` | `/run/secrets/app_secret_key` | Mounted file with at least 32 random bytes | Secret used to sign opaque pagination cursors. |
| `GITHUB_PRIVATE_KEY_FILE` | `/run/secrets/github_app_private_key` | PEM file | GitHub App private key; mounted into `worker` only. |
| `GITHUB_TOKEN_FILE` | empty | Optional mounted file | Optional fine-grained PAT, restricted to the knowledge repository. Takes precedence over the App token. |
| `GITHUB_WEBHOOK_SECRET_FILE` | `/run/secrets/github_webhook_secret` | Mounted file | HMAC secret for optional GitHub pull-request webhooks. |
| `DATABASE_URL` | `sqlite+aiosqlite:////app/data/agenthub.db` | SQLite+aiosqlite URL | Operational SQLite database; keep it on a local Docker volume. |
| `ALLOW_UNSAFE_DB_FS` | `false` | boolean | Override the network/FUSE filesystem startup refusal only for controlled testing. |
| `GITHUB_APP_ID` | empty | GitHub App numeric ID | GitHub App identity. |
| `GITHUB_INSTALLATION_ID` | empty | GitHub installation ID | Installation granted access to the single knowledge repository. |
| `GITHUB_REPOSITORY_OWNER` | empty | GitHub owner/org | Owner of the private knowledge repository. |
| `GITHUB_KNOWLEDGE_REPOSITORY` | `agenthub-knowledge` | Repository name | Canonical knowledge repository. |
| `GITHUB_DEFAULT_BRANCH` | `main` | Branch name | Expected default branch and refresh target. |
| `GITHUB_PR_POLL_INTERVAL_SECONDS` | `300` | Positive seconds | Worker reconciliation interval; reconciles once on startup. |
| `GITHUB_WEBHOOKS_ENABLED` | `false` | boolean | Enable the signature-verified webhook accelerator. Polling remains primary. |
| `PUBLISH_MAX_ATTEMPTS` | `5` | 1 or greater | Maximum transient artifact publish attempts before failure. |
| `TASK_LEASE_SECONDS` | `120` | Positive seconds | Lease extension on task claim and heartbeat. |
| `TASK_HEARTBEAT_SECONDS` | `40` | Positive seconds | Recommended agent heartbeat interval. |
| `TASK_MAX_ATTEMPTS` | `3` | 1–10 | Default task attempt limit. |
| `TASK_RETRY_BACKOFF_SECONDS` | `30` | Positive seconds | Initial retry delay, doubled each attempt. |
| `TASK_RETRY_BACKOFF_MAX_SECONDS` | `600` | Positive seconds | Maximum task retry delay. |
| `TASK_MAX_RUNTIME_SECONDS` | `14400` | Positive seconds | Maximum runtime per lease generation. |
| `WORKER_POLL_INTERVAL_SECONDS` | `10` | Positive seconds | Lease reaper interval. |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `5` | Positive seconds | Adapter outbox dispatcher interval. |
| `OUTBOX_PROCESSING_TIMEOUT_SECONDS` | `120` | Positive seconds | Time before a stuck `processing` event is recovered. |
| `OUTBOX_MAX_ATTEMPTS` | `8` | Positive integer | Outbox failure threshold. |
| `SSE_MAX_CONNECTIONS` | `20` | Positive integer | Maximum concurrent event streams. |
| `MAX_REQUEST_BYTES` | `1048576` | Positive bytes | Overall REST/MCP request limit. |
| `MAX_TASK_PAYLOAD_BYTES` | `65536` | Positive bytes | Maximum serialized task payload. |
| `MAX_MESSAGE_BYTES` | `65536` | Positive bytes | Maximum UTF-8 message body. |
| `MAX_ARTIFACT_BYTES` | `262144` | Positive bytes | Maximum UTF-8 artifact body. |
| `RATE_LIMIT_AGENT_PER_MINUTE` | `120` | Positive requests/minute | In-memory sliding window per agent token. |
| `RATE_LIMIT_ANON_PER_MINUTE` | `600` | Positive requests/minute | In-memory per-client-address ceiling, including unauthenticated and invalid-credential traffic. |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `43200` | Positive seconds | Browser session idle expiry. |
| `SESSION_ABSOLUTE_TIMEOUT_SECONDS` | `604800` | Positive seconds | Maximum browser session lifetime. |
| `EVENT_RETENTION_DAYS` | `30` | Positive days | Delivered and not-required outbox event retention. |
| `FAILED_EVENT_RETENTION_DAYS` | `90` | Positive days | Permanently failed outbox event retention. |
| `TASK_EVENT_RETENTION_DAYS` | `180` | Positive days | Events for terminal tasks. |
| `AUDIT_RETENTION_DAYS` | `365` | Positive days | Audit history retention. |
| `GITHUB_DELIVERY_RETENTION_DAYS` | `30` | Positive days | Webhook delivery deduplication retention. |
| `BACKUP_DIR` | `./backups` | Writable host directory | Daily backup location, mounted only in the worker. |
| `BACKUP_TIME_UTC` | `03:15` | `HH:MM` UTC | Daily backup time. |
| `BACKUP_RETENTION_DAYS` | `14` | Positive days | Number of local daily backups to keep. |

## Transport modes

- **TLS:** The supplied `web` image listens on HTTP port 80 by default. For TLS at the Nginx container, add `deploy/compose.tls.yaml`, mount `secrets/tls_certificate.pem` and `secrets/tls_private_key.pem`, set an HTTPS `PUBLIC_BASE_URL`, `COOKIE_SECURE=true`, and allow the exact host and origin. A trusted external TLS proxy is also supported. Only expose the LAN/VPN interface.
- **Plain HTTP LAN:** Set `COOKIE_SECURE=false` and use the reserved LAN address. The app logs a prominent warning because cookies and bearer tokens cross the LAN unencrypted. Never expose this mode publicly.

On MCP calls, `Host` must exactly match an `ALLOWED_HOSTS` entry. When present, `Origin` must exactly match `ALLOWED_ORIGINS`; omit `Origin` for non-browser MCP clients.
