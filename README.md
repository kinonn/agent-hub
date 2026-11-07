# AgentHub

AgentHub is a self-hosted coordination and project-knowledge service for AI agents. SQLite owns operational state; a private GitHub repository is the durable knowledge store; MCP is the primary agent interface.

The project is bootstrapped to the **AgentHub MVP Raspberry Pi / SQLite specification v1.5**. It includes the FastAPI + MCP backend, SQLite/Alembic schema, task/message/artifact services, background worker, React dashboard, Docker Compose deployment, CLI, and acceptance-oriented tests.

## Quick start

Target: Raspberry Pi OS 64-bit, Docker Compose v2, a reserved LAN address, and a local filesystem-backed Docker volume (prefer a USB SSD).

1. Clone this repository and run `./scripts/init-secrets.sh`.
2. Edit `.env`: set `LAN_BIND_IP`, `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, and `ALLOWED_ORIGINS`. For plain HTTP LAN mode use `COOKIE_SECURE=false`; for TLS termination set the public HTTPS URL and `COOKIE_SECURE=true`.
3. Add the GitHub App private key and configure `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, and `GITHUB_REPOSITORY_OWNER`. Artifacts require the repository owner to be configured; publication waits until credentials are available. A PAT may be mounted at `secrets/github_pat.txt` and enabled by setting `GITHUB_TOKEN_FILE=/run/secrets/github_pat`.
4. Build the dashboard image on a workstation with Docker Buildx for `linux/arm64`, then transfer it to the Pi or push it to a registry and set `WEB_IMAGE` in `.env`:

   ```sh
   docker buildx build --platform linux/arm64 --load -t agenthub-web:local -f frontend/Dockerfile frontend
   docker save agenthub-web:local | ssh pi-user@192.168.1.10 docker load
   ```

5. Build the Python backend and start the stack:

   ```sh
   docker compose build app-core worker
   docker compose up -d
   ```

6. Provision the first administrator interactively (no default password exists):

   ```sh
   docker compose exec app-core python -m src.cli create-admin --email you@example.com
   ```

7. Create an agent and project, then grant membership:

   ```sh
   docker compose exec app-core python -m src.cli create-agent --name Hermes --agent-type hermes --capability research
   docker compose exec app-core python -m src.cli create-project --slug my-project --name "My Project"
   docker compose exec app-core python -m src.cli add-member my-project <agent-id> --role contributor
   ```

Open `http://<LAN_BIND_IP>:<WEB_PORT>` for the dashboard. AgentHub is not intended to be exposed directly to the public internet; use a VPN for remote access.

For TLS terminated by the supplied Nginx container, install a certificate/key as `secrets/tls_certificate.pem` and `secrets/tls_private_key.pem`, set the HTTPS URL/allowlists and `COOKIE_SECURE=true`, then start with `docker compose -f compose.yaml -f deploy/compose.tls.yaml up -d`. Port 80 redirects to HTTPS; the overlay publishes port 443.

## Agent access

- REST: `<PUBLIC_BASE_URL>/api/v1`
- MCP Streamable HTTP: `<PUBLIC_BASE_URL>/mcp`
- Authentication: `Authorization: Bearer ah_<prefix>_<secret>`
- Configure the token as a secret header in the client. Do not commit it or place it in an MCP URL.

See [MCP tools](docs/mcp-tools.md) and [Hermes integration notes](docs/hermes-integration.md). The generic polling example is `scripts/poll_client.py`; it requires an explicitly configured trusted task handler before claiming work.

## Development

Python 3.12+ and Node 22+ are used. From the project root:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
mkdir -p data
APP_ENV=development DATABASE_URL=sqlite+aiosqlite:///./data/agenthub.db alembic upgrade head
APP_ENV=development DATABASE_URL=sqlite+aiosqlite:///./data/agenthub.db uvicorn src.main:app --reload
```

Build the frontend with `cd frontend && npm ci && npm run build`. Run tests with `pytest`.

## Operations

- `python -m src.cli check-db` verifies integrity, WAL mode, SQLite extensions, filesystem, and Alembic revision.
- `python -m src.cli backup` writes a verified, compressed WAL-aware backup.
- `python -m src.cli restore <backup-file>` restores a backup while the stack is stopped.
- `python -m src.cli reindex` rebuilds the FTS5 index. In a Compose deployment, run `reindex --refresh` in `worker` (`docker compose exec worker python -m src.cli reindex --refresh`) because only that container has GitHub credentials.

See [configuration](docs/configuration.md) and [operations](docs/operations.md) for full details.

## Deployment status

The source and frontend are validated locally; Raspberry Pi resource usage, Docker cgroup enforcement, GitHub App permissions, and client-specific Hermes/OpenCode/Copilot configuration still require verification in the target deployment environment.
