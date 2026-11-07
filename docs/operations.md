# Operations

## First deployment

1. Use a reserved Pi LAN address and a reliable local SSD. SQLite must not live on NFS, SMB/CIFS, SSHFS, or another FUSE/network filesystem.
2. Run `./scripts/init-secrets.sh`; it grants UID/GID 10001 access to backend secrets/backups and UID/GID 101 access to TLS secrets. Replace backend credentials with `sudo install -o 10001 -g 10001 -m 0400 <source> secrets/<name>` and TLS files with `sudo install -o 101 -g 101 -m 0400 <source> secrets/<tls-file>`. Edit `.env`; configure the GitHub App's Contents and Pull requests permissions for the single private `agenthub-knowledge` repository.
3. Protect the repository default branch with required pull requests and at least one approving review. The App must not bypass protection; add a maintainer as a CODEOWNER.
4. Build the web image off the Pi for `linux/arm64` and load it on the Pi or publish it to a registry; set `WEB_IMAGE` accordingly. Build only the backend on the Pi with `docker compose build app-core worker`, then run `docker compose up -d`.
5. Provision the first admin with `docker compose exec app-core python -m src.cli create-admin --email you@example.com`.
6. Confirm `docker compose ps`, `docker compose logs app-core worker`, and `docker inspect --format '{{.HostConfig.Memory}}'` for the three container limits.
7. Provision agent(s) and project membership with the CLI or admin Settings page. Raw agent tokens are displayed once.

For Nginx TLS termination, install a valid certificate/key in the mounted secret files, set `COOKIE_SECURE=true` and HTTPS host/origin values, then use `docker compose -f compose.yaml -f deploy/compose.tls.yaml up -d`.

Migrations are run only by `app-core` in `deploy/entrypoint-app.sh`. The worker starts after `app-core` passes its health check and verifies the schema; it never runs Alembic.

## CLI commands

Run ordinary database and provisioning commands inside `app-core` with `docker compose exec app-core python -m src.cli <command>` (or `docker compose run --rm app-core ...` after the stack is initialized).

| Command | Purpose |
|---|---|
| `create-admin --email EMAIL` | One-time first administrator; prompts twice for a password. `--password-file` is available for automation. |
| `create-user --email EMAIL --display-name NAME` | Provision a dashboard user. |
| `create-agent --name NAME [--agent-type TYPE] [--capability CAP]` | Create an agent; prints its raw token once. |
| `rotate-token AGENT_ID` | Revoke active agent tokens and print a new token once. |
| `revoke-token AGENT_ID TOKEN_ID` | Revoke one token. |
| `disable-agent AGENT_ID` | Disable authentication and expire the agent's active leases for normal reaping. |
| `create-project --slug SLUG --name NAME` | Create a project. |
| `add-member PROJECT AGENT_ID --role ROLE` | Add an agent to a project. |
| `backup` | Use SQLite's online backup API, run integrity check, compress, and prune old backups. |
| `restore BACKUP_FILE` | Restore a verified `.sqlite` or `.sqlite.gz` file; stop the stack first. |
| `check-db` | Verify SQLite, FTS5, JSON1, WAL, Alembic version, and integrity. |
| `reindex` | Rebuild the derived FTS5 index; run in `app-core`. |
| `reindex --refresh` | Re-fetch published content from GitHub, then rebuild FTS5; run with `docker compose exec worker python -m src.cli reindex --refresh` because GitHub credentials are mounted only into `worker`. |

Do not mount GitHub credentials into `app-core` just to run `reindex --refresh`; keeping GitHub access in the worker is intentional.

## Backup and restore

Backups are independent of GitHub; they include operational metadata and staged artifact content. They are owned by container UID 10001; use a root/sudo-managed sync job to copy encrypted backups off the Pi where practical.

Restore procedure:

1. `docker compose stop web worker app-core`.
2. Run `docker compose run --rm app-core python -m src.cli restore /app/backups/<backup-file>`.
3. The restore command verifies the backup, then atomically preserves the current `.db`, `-wal`, and `-shm` files with a `.pre-restore-<timestamp>` suffix before installing the restored copy.
4. Run `docker compose up -d`, then `docker compose exec app-core python -m src.cli check-db`.
5. Let GitHub reconciliation run, then `docker compose exec app-core python -m src.cli reindex`.

Never copy only the live `.db` file while WAL mode is active. Automated backups use Python's `sqlite3.Connection.backup()` API.

## Health and resource checks

- `GET /health` is an unauthenticated liveness check.
- `GET /health/details` is admin-only and reports SQLite WAL size, worker heartbeat age, task counts, and outbox counts.
- Compose hard limits are `512m` (`app-core`), `384m` (`worker`), and `128m` (`web`), 1,024 MB total.
- On the Pi, verify memory cgroups with `docker info`, inspect effective limits with `docker inspect`, and record `docker stats --no-stream` under representative workload. Confirm worker heartbeat and restart recovery after an OOM kill.
- The app runs `PRAGMA wal_checkpoint(PASSIVE)` periodically and after backup when idle, plus daily `PRAGMA optimize`; it does not auto-run `VACUUM`.

## Safety

Never expose the Pi directly to the public internet. Use a VPN for remote access. Do not put tokens, password values, message bodies, task payloads, or artifact content in logs, audit metadata, shell commands, or adapter command lines. Adapter configuration may contain only non-secret configuration and secret file references.
