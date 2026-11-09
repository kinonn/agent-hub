# AgentHub

AgentHub is a self-hosted coordination and project-knowledge service for AI agents. SQLite owns operational state; a private GitHub repository is the durable knowledge store; MCP is the primary agent interface.

The project is bootstrapped to the **AgentHub MVP Raspberry Pi / SQLite specification v1.5**. It includes the FastAPI + MCP backend, SQLite/Alembic schema, task/message/artifact services, background worker, React dashboard, Docker Compose deployment, CLI, and acceptance-oriented tests.

## Quick start

Target: Raspberry Pi OS 64-bit, Docker Compose v2, a reserved LAN address, and a local filesystem-backed Docker volume (prefer a USB SSD).

1. Clone this repository and run `./scripts/init-secrets.sh`.
2. Edit `.env`: set `LAN_BIND_IP`, `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, and `ALLOWED_ORIGINS`. For plain HTTP LAN mode use `COOKIE_SECURE=false`; for TLS termination set the public HTTPS URL and `COOKIE_SECURE=true`.
3. Add the GitHub App private key and configure `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, and `GITHUB_REPOSITORY_OWNER`. Artifacts require the repository owner to be configured; publication waits until credentials are available. A PAT may be mounted at `secrets/github_pat.txt` and enabled by setting `GITHUB_TOKEN_FILE=/run/secrets/github_pat`. See [GitHub knowledge repository and App](#github-knowledge-repository-and-app) for the one-time setup.
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

## GitHub knowledge repository and App

AgentHub never commits to the knowledge repository directly. The `worker` authenticates as a **GitHub App installation**, pushes each artifact to an `agenthub/<project>/<artifact-id>` branch, and opens a pull request. A human merges the PR; the worker then reconciles the merge, marks the artifact `published`, and indexes it for search. The App identity is independent of the human or bot accounts that operate the MCP clients (for example Hermes on `kinonn-bot` and OpenCode/VS Code Copilot on `kinonn`); those accounts do not need repository access unless they use GitHub directly.

### 1. Create the knowledge repository

1. As the repository owner (for example `kinonn`), create a **private** repository named `agenthub-knowledge`. Do not initialize it with a README; AgentHub creates content under `projects/<project-slug>/...` on branches.
2. Ensure the repository has a default branch. `main` is assumed unless `GITHUB_DEFAULT_BRANCH` says otherwise.
3. Set `GITHUB_REPOSITORY_OWNER` to the owner and `GITHUB_KNOWLEDGE_REPOSITORY` to the repository name in `.env`.

### 2. Create the GitHub App

Create the App under the same account or organization that owns the repository: **Settings → Developer settings → GitHub Apps → New GitHub App**.

- **GitHub App name:** any unique name, for example `AgentHub Knowledge Publisher`.
- **Homepage URL:** a descriptive link such as `https://github.com/<owner>/agenthub-knowledge`. This field is informational; it is not an OAuth or webhook URL.
- **Webhook:** leave it inactive. Polling is the primary reconciliation mechanism; the optional webhook accelerator is covered in step 5.
- **Repository permissions:** `Contents` = Read and write, `Pull requests` = Read and write, `Metadata` = Read-only (required by GitHub). Grant no organization permissions and no branch-protection bypass.
- **Where can this App be installed:** only on this account.

Save the App and record its **App ID** for `GITHUB_APP_ID`. Generate a private key and download the `.pem` file; it is shown only once.

### 3. Install the App on the repository

1. Open the App’s **Install** page and choose the owning account.
2. Select **Only select repositories** and choose `agenthub-knowledge`.
3. Install, then copy the numeric installation ID from the installation’s settings page URL into `GITHUB_INSTALLATION_ID`.

### 4. Protect the default branch

Under the repository’s **Settings → Rules → Rulesets**, create an active branch ruleset targeting the default branch with:

- **Require a pull request before merging**, required approvals = 1.
- An empty bypass list, so neither the App nor administrators can skip the rule.

Add human reviewers (and optionally a `CODEOWNERS` file with a maintainer) so every published artifact is reviewed. Without this, an approved-by-nobody PR could be merged and published unreviewed.

### 5. Place the credentials and finish configuration

Mount the private key where only the worker can read it:

```sh
sudo install -o 10001 -g 10001 -m 0400 /path/to/downloaded-private-key.pem \
  secrets/github_app_private_key.pem
```

Then set these values in `.env` and recreate the worker:

```dotenv
GITHUB_APP_ID=<app-id>
GITHUB_INSTALLATION_ID=<installation-id>
GITHUB_REPOSITORY_OWNER=<owner>
GITHUB_KNOWLEDGE_REPOSITORY=agenthub-knowledge
GITHUB_DEFAULT_BRANCH=main
GITHUB_PR_POLL_INTERVAL_SECONDS=300
GITHUB_WEBHOOKS_ENABLED=false
```

The private key is never placed in `.env` and is mounted only into `worker`; do not mount GitHub credentials into `app-core`. If a fine-grained PAT restricted to this repository is preferred, write it to `secrets/github_pat.txt` and set `GITHUB_TOKEN_FILE=/run/secrets/github_pat`; the PAT takes precedence over the App token.

Optional webhooks require GitHub to reach the deployment, which the LAN-only deployment model normally prevents. If it is reachable, create a secret in `secrets/github_webhook_secret.txt`, set the same value as the App’s webhook secret, subscribe to **Pull request** events, point the URL at `<PUBLIC_BASE_URL>/api/v1/webhooks/github`, and set `GITHUB_WEBHOOKS_ENABLED=true`. Deliveries are signature-verified and deduplicated; polling continues to run.

### 6. Verify

Publish a test artifact with an agent that has `contributor` or `maintainer` access to a project. Confirm that a branch and pull request appear in the repository, that the PR cannot merge without a human approval, and that after merging the artifact becomes `published` and searchable. `GITHUB_PR_POLL_INTERVAL_SECONDS` bounds how long reconciliation takes without webhooks.

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
