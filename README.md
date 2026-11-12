# WhirlpoolBee

Self-hosted Claude Code usage analytics for individuals and teams.

WhirlpoolBee collects usage data from developer machines and displays it in a
FastAPI web application backed by SQLite. You can run everything on one machine
or host a central dashboard with agents on macOS, Linux, and Windows.

The project is [MIT licensed](LICENSE). Any organization can use, modify, and
self-host it with its own server, accounts, and optional identity provider.
There is no required company account, email domain, or hosted dashboard service.

> **Before collecting data:** agents can send conversation content, tool activity,
> file paths, and code-edit snippets to your dashboard, not just usage counts.
> Read [Data and privacy](#data-and-privacy) and obtain the appropriate consent
> before deploying agents.

## Contents

- [What the application provides](#what-the-application-provides)
- [Choose a deployment](#choose-a-deployment)
- [Prerequisites](#prerequisites)
- [Get the source and configure the application](#get-the-source-and-configure-the-application)
- [Run directly with Python](#run-directly-with-python)
- [Run with Docker](#run-with-docker)
- [Configure HTTPS and shared access](#configure-https-and-shared-access)
- [Set up administrators and login](#set-up-administrators-and-login)
- [Install client agents](#install-client-agents)
- [Verify an end-to-end installation](#verify-an-end-to-end-installation)
- [Configuration reference](#configuration-reference)
- [Optional Azure DevOps integration](#optional-azure-devops-integration)
- [Data and privacy](#data-and-privacy)
- [Logs and service management](#logs-and-service-management)
- [Backups, upgrades, and recovery](#backups-upgrades-and-recovery)
- [Troubleshooting](#troubleshooting)
- [Development and project layout](#development-and-project-layout)

## What the application provides

| Area | Capabilities |
|---|---|
| Usage | Sessions, token consumption, estimated costs, and activity trends |
| Teams | Machine registration, team grouping, scoped user access, and benchmarking |
| Insights | Session tagging, anomalies, learning metrics, and project analytics |
| Conversations | Session details, tool activity, and restricted tone analysis |
| Ask | Local keyword/time-range parsing of questions about collected usage |
| Integrations | Optional Azure DevOps pull-request and commit analysis |
| Administration | Login providers, local accounts, users, and synchronization status |

Available metrics depend on the source data and collectors installed. Token
costs are estimates based on the pricing tables in the code, not billing records.
AI-assisted pull-request attribution is a heuristic, not proof of authorship.

## Choose a deployment

| Goal | Server | Agents | Access |
|---|---|---|---|
| Evaluate on one computer | Python on macOS/Linux | One local agent | Loopback HTTP without login |
| Share with a team | Docker or a managed Python process | One per developer machine/user environment | HTTPS, authentication, and network restrictions |
| Monitor Windows clients | Server hosted separately or in Docker | Native PowerShell installer | Your dashboard's HTTPS URL |

The server stores and displays data. Starting it does **not** automatically
collect Claude Code sessions from the host. Run an agent on every machine whose
sessions should appear, including the server host if applicable.

Use one server process/container with a persistent SQLite database initially.
This guide does not configure replicated servers or a shared database cluster.

## Prerequisites

| Component | Requirement |
|---|---|
| Python server | Python 3.11 or 3.12, `pip`, and `venv`; 3.12 matches the Docker image |
| Docker server | Docker Engine/Desktop with a working daemon; Compose v2 if using Compose |
| Clients | Python available on `PATH`; use 3.11 or 3.12 for consistency |
| macOS/Linux installer | Bash and `curl`; Linux uses a systemd user service where available |
| Windows installer | PowerShell 5.1+ and a real Python installation, not only a Store alias |
| Source checkout | Git, or an extracted repository archive |
| Data collection | Claude Code session data readable by the user running the agent |
| Shared deployment | DNS/hostname, trusted TLS certificate, reverse proxy, and firewall rules |

Dependency installation and container builds need internet access or an internal
package/image mirror. If Python package compilation fails, install the platform's
compiler and Python development headers. Prefer the versions above with the
repository's pinned dependencies.

Run agents as the developer who uses Claude Code, not as root or an unrelated
service account. Home-directory discovery depends on the current user.

## Get the source and configure the application

### 1. Obtain the project

Replace `<repository-url>` with the URL of your fork or this project's repository:

```bash
git clone <repository-url> claude-local-dashboard
cd claude-local-dashboard
```

If you already have the source, open a terminal in the directory containing
`requirements.txt`, `schema.sql`, and `src/`. Commands below assume this directory
unless stated otherwise.

### 2. Create a private configuration file

For a **new deployment only**:

```bash
cp .env.example .env
chmod 600 .env
```

Do not overwrite an existing deployment's `.env`. It contains the key needed to
decrypt saved login-provider secrets.

Generate a session secret:

```bash
openssl rand -hex 32
```

Alternatively, with Python:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edit `.env` and set your generated value and your initial administrator's email:

```dotenv
DASHBOARD_SESSION_SECRET=replace-with-your-generated-secret
DASHBOARD_ADMIN_EMAILS=admin@example.com
```

Replace the example email with the exact email that the administrator will use
for SSO or a local account. This allow-list grants authorization; it does **not**
create an account or enable login.

Leave `DASHBOARD_AUTH_ENABLED` unset during initial configuration. On a fresh
database, the application is open until an enabled provider or a local account
exists. Keep it reachable only by the operator during this setup window.

`.env` is ignored by Git and excluded from the Docker image. Keep it private and
back it up securely. Do not copy another organization's `.env`, database, or
collected session data into a new deployment.

## Run directly with Python

These commands are for macOS/Linux. Windows users can host the server with Docker
and use the native Windows client installer below.

### 1. Create a virtual environment and install dependencies

```bash
python3 --version
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

No separate database server, Node.js installation, or frontend build is required.

### 2. Start the dashboard

For a new single-machine installation, keep the server database separate from
the legacy collector database that the agent searches for:

```bash
DASHBOARD_DATA_DIR="$HOME/.whirlpoolbee-server" ./venv/bin/python -m src.api.main
```

Startup creates the configured data/log directories and initializes or migrates
the database. This command stores server data in `~/.whirlpoolbee-server` and
assumes `DASHBOARD_DB_PATH` is unset. Use the same data-directory setting on every
start, or persist its absolute path as `DASHBOARD_DATA_DIR` in `.env`.

The separation matters because the agent prefers an existing
`~/.claude-dashboard/claude.db` over JSONL files. Starting a fresh server at that
default path can cause the local agent to read the empty destination database
instead of Claude Code sessions. Do not delete an existing collector database;
review its contents and intended role first.

With the default bind address and port, open:

```text
http://127.0.0.1:8765
```

In another terminal:

```bash
curl --fail --silent --show-error http://127.0.0.1:8765/api/health
```

The JSON response should contain `"status":"healthy"`. This confirms the web
server responds; it does not confirm that an agent has collected any sessions.

Press `Ctrl+C` in the server terminal to stop it. For a persistent deployment,
run the same command under your platform's service manager with the repository
as its working directory and the correct environment/data-directory permissions.

### 3. Send local session data

Keep the server running and, in another terminal in the project directory, run:

```bash
./venv/bin/python client_agent.py \
  --server http://127.0.0.1:8765 \
  --debug
```

`--debug` keeps the agent in the foreground and permits `Ctrl+C`. Without it, the
agent is intended for service operation and ignores common termination signals.

The agent registers the machine, discovers session data, and syncs periodically.
The default interval is 300 seconds. A machine can register even if no session
data has been found yet.

For persistent collection, use the [client installer](#install-client-agents)
instead of leaving this terminal running. Avoid running multiple agents for the
same machine.

### Existing macOS helper scripts

`install.sh` is a legacy macOS-only installer. It recreates the repository's
`venv`, installs launchd collectors, and modifies `~/.zshrc`. Prefer the manual
server setup and standalone agent above for a predictable initial installation.
The legacy git-metrics launchd job references a collector script not present in
this checkout, and its shell wrapper does not implement session logging.

For an existing helper-script installation, `./start.sh` backgrounds the server,
`./stop.sh` stops it, and server output goes to
`~/.claude-dashboard/logs/dashboard.log`. `./start_server.sh` instead runs in the
foreground on **all network interfaces**, port 8765. Do not use that mode before
configuring shared-access protections.

## Run with Docker

Complete the `.env` setup first. Docker hosts the server only; run agents on the
actual developer machines rather than mounting their entire home directories
into the container.

For Docker, leave `DASHBOARD_DATA_DIR` and `DASHBOARD_DB_PATH` unset in `.env`
so the image defaults use `/app/data` and `/app/data/claude.db`. If overriding
them, use paths inside the mounted container volume, not host filesystem paths.

The Docker CLI example uses Bash syntax. On Windows, use a Bash-compatible shell
or the Compose option, whose commands also work in PowerShell.

> **Current packaging requirement:** the Dockerfile does not copy
> `schema_v4.sql`, which defines login providers and local accounts. The commands
> below mount that file read-only so a fresh container has the required schema.
> Keep this mount until the image packaging includes it.

### Option A: Docker CLI

From the project directory:

```bash
docker build -t whirlpoolbee:local .
docker volume create whirlpoolbee-data
docker run -d \
  --name whirlpoolbee \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8765:8765 \
  -v whirlpoolbee-data:/app/data \
  --mount "type=bind,src=$(pwd)/schema_v4.sql,dst=/app/schema_v4.sql,readonly" \
  whirlpoolbee:local
```

The published port is intentionally loopback-only. A reverse proxy on the same
host can forward HTTPS requests to it. Remote clients cannot use the host's
loopback address directly.

```bash
docker logs --tail 100 whirlpoolbee
curl --fail --silent --show-error http://127.0.0.1:8765/api/health
```

### Option B: Docker Compose

Create `compose.local.yaml` in the project directory with the following content:

```yaml
services:
  dashboard:
    build: .
    image: whirlpoolbee:local
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "127.0.0.1:8765:8765"
    volumes:
      - whirlpoolbee-data:/app/data
      - ./schema_v4.sql:/app/schema_v4.sql:ro

volumes:
  whirlpoolbee-data:
```

Use this as a standalone Compose file:

```bash
docker compose -f compose.local.yaml up -d --build
docker compose -f compose.local.yaml logs --tail 100 dashboard
curl --fail --silent --show-error http://127.0.0.1:8765/api/health
```

This example deliberately differs from the checked-in `docker-compose.yml`,
which publishes port 8765 on all interfaces and does not mount `schema_v4.sql`.
Do not combine the files without reviewing their merged port/volume settings.

Compose normally prefixes the volume name with the project name. It is not
automatically the same volume as the Docker CLI example. Choose one method and
keep using it for that deployment.

## Configure HTTPS and shared access

Authenticated browser sessions use cookies marked `Secure` and `SameSite=None`.
**Use HTTPS for login, including local username/password accounts.** Plain
loopback HTTP is suitable for a local, unauthenticated evaluation, not a shared
authenticated installation.

Use your organization's reverse proxy or ingress with a trusted TLS certificate.
For example, the following is an nginx location inside an **already configured
HTTPS server block**, with nginx and the dashboard on the same host:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

This is not a complete TLS/nginx configuration. If the proxy runs in another
container, its `127.0.0.1` refers to itself; use a private container network and
the dashboard's service name instead.

Before granting team access:

1. Restrict access to the operator while configuring the first login account.
2. Verify the HTTPS URL and forwarded host/protocol headers.
3. Configure and test administrator login.
4. Restrict direct access to the backend port; only the trusted proxy should
   supply forwarded headers.
5. Apply network restrictions appropriate for agent registration and ingestion.

For deployment under a path such as `https://dashboard.example.com/analytics`,
set `ROOT_PATH=/analytics`. Configure the proxy to strip that prefix before
forwarding to the application. Include the prefix in client `--server` URLs,
static installer URLs, and identity-provider callback URLs. `ROOT_PATH` alone
does not configure proxy rewriting.

## Set up administrators and login

### Bootstrap the first administrator

Make sure `.env` contains your intended administrator email in
`DASHBOARD_ADMIN_EMAILS` **before starting the server**. Restart the server after
changing this environment variable.

From the restricted HTTPS deployment, open **Admin -> Login Providers**
(`/admin/login-providers`). Choose either a local account or an SSO provider.

As soon as an enabled provider or any local-account row exists, login is required
unless explicitly overridden. An existing but disabled local account still
causes authentication to be required. Do not enable a login method before you
have its credentials and callback configuration ready.

### Local username/password account

1. Under **Local Accounts**, select **Add account**.
2. Enter the same email configured in `DASHBOARD_ADMIN_EMAILS`, a display name,
   and a strong password. The API requires at least eight characters.
3. Leave the account enabled and save.
4. Open `/auth/login` and sign in with that email and password.
5. Confirm that the administration pages remain accessible.

An administrator email entry does not have a default password. Accounts are
created by administrators; there is no public self-service registration.

### SSO providers

Create an application/OAuth client in your own identity provider, then add its
configuration under **Admin -> Login Providers**.

| Provider | Required settings | Callback for the example provider key |
|---|---|---|
| Google (`google`) | Client ID, client secret | `https://dashboard.example.com/auth/callback/google` |
| GitHub (`github`) | OAuth App client ID and secret | `https://dashboard.example.com/auth/callback/github` |
| Microsoft / Azure AD (`azure-ad`) | Client ID, client secret, tenant ID | `https://dashboard.example.com/auth/callback` |
| Generic OAuth2 (`company-sso`) | Client ID/secret, authorization URL, token URL, userinfo URL, scopes | `https://dashboard.example.com/auth/callback/company-sso` |

Replace the hostname and, if applicable, add `ROOT_PATH` before `/auth/callback`.
For non-Azure providers, the final callback segment is the **provider key** you
configured, not its display name. Use `azure-ad` for the Microsoft provider,
whose callback has no final key segment.

The generic integration uses an OAuth2 authorization-code flow and a userinfo
endpoint; supply those URLs explicitly rather than an OIDC discovery URL. The
userinfo response must expose an email. Google uses `openid email profile`;
GitHub uses `read:user user:email`. Use provider access policies to control who
may authenticate.

Login-provider settings are stored in the database, and client secrets are
encrypted using a key derived from `DASHBOARD_SESSION_SECRET`. Keep that value
stable across restarts and restores. New deployments should use the UI rather
than the legacy `AZURE_AD_*` environment variables.

### Authorization and machine visibility

Administrators can view all machines and manage users/providers. Other signed-in
users are scoped to machines matched to their identity. A user with no matching
machine sees a no-machine page or an API `403`.

The agent tries Git email and OS identity information when registering. Check
that the reported identity corresponds to the user's login. Administrators can
review machine/user assignments in the dashboard.

`DASHBOARD_SUPER_USER_EMAILS` seeds an independent allow-list for restricted
features such as Conversation Tone. Administrator status alone does not grant
this additional permission.

Environment allow-lists insert entries at startup; removing an email from
`.env` does not remove a previously stored entry. Manage existing access in the
administration UI/API.

## Install client agents

Run an agent on each developer's machine, under that developer's account.
The central server does not need Claude Code installed unless you also want
to collect sessions from that host.

Use the **external dashboard URL**, including any path prefix. A remote
machine's `localhost` is not the dashboard host.

### macOS and Linux

Download and inspect the installer before running it:

```bash
curl --fail --show-error --location \
  "https://dashboard.example.com/static/client-install.sh" \
  --output client-install.sh
```

After reviewing the downloaded file:

```bash
bash client-install.sh --server "https://dashboard.example.com"
```

The installer creates `~/.claude-dashboard-agent`, installs dependencies into its
own virtual environment, and configures launchd on macOS or a systemd user service
on supported Linux installations. Where service setup is unavailable, inspect
the installer output/log for fallback behavior.

Linux user services normally depend on the user's login/session configuration;
running continuously after logout may require administrator-approved user
lingering. A successful install is not a guarantee of system-wide boot startup.

The installer also accepts `CLAUDE_DASHBOARD_SERVER` when `--server` is omitted.
There is no default hosted server.

### Windows PowerShell

Install Python with `pip` and make it available on `PATH`. Then download and
inspect the native installer:

```powershell
Invoke-WebRequest `
  -Uri "https://dashboard.example.com/static/client-install.ps1" `
  -OutFile ".\client-install.ps1"
```

After reviewing it, run:

```powershell
.\client-install.ps1 -ServerUrl "https://dashboard.example.com"
```

Follow your organization's PowerShell execution policy if local scripts are
blocked; do not disable machine-wide security policy just to install the agent.
The native installer does not require Git Bash or WSL.

It installs under `%USERPROFILE%\.claude-dashboard-agent` and attempts to register
the per-user tasks `ClaudeDashboardAgent` and `ClaudeDashboardAgentWatchdog`.
The installer also accepts `$env:CLAUDE_DASHBOARD_SERVER` instead of `-ServerUrl`.

### Debugging and nonstandard data locations

The installer supports `--debug` (Bash) or `-DebugMode` (PowerShell) for foreground
operation. **Rerunning an installer is a clean reinstall:** it removes the
existing agent installation directory, including logs and `.agent_token`.
Back up that token privately before reinstalling an already registered agent.
After reinstalling, restore it beside `client_agent.py` and restart the agent,
or arrange server-side token recovery with an administrator. Do not use a
reinstall as the first troubleshooting step.

For a manual foreground run from the source checkout:

```bash
./venv/bin/python client_agent.py \
  --server "https://dashboard.example.com" \
  --interval 60 \
  --jsonl-path "$HOME/.claude/projects" \
  --debug
```

| Agent option | Meaning |
|---|---|
| `--server` | Required dashboard URL |
| `--interval` | Sync interval in seconds; default `300` |
| `--jsonl-path` | Override the JSONL discovery directory |
| `--debug` | Foreground operation with `Ctrl+C` support |

The agent supports SQLite sources and Claude Code JSONL files, commonly under
`~/.claude/projects` or `%USERPROFILE%\.claude\projects`. SQLite discovery takes
precedence over JSONL discovery, even when `--jsonl-path` is provided; that option
changes the JSONL directory, not the source priority. It reports missing sources
in its logs and the dashboard's sync status rather than inventing data.

Registration issues a per-machine token, stored in `.agent_token` beside the
agent script. Subsequent writes use that token. Keep it private and do not share
one machine's token with another.

## Verify an end-to-end installation

1. Request `/api/health` and confirm `"status":"healthy"`.
2. For shared deployments, sign in over HTTPS and confirm the administrator can
   open **Login Providers** and user-management pages.
3. Start one agent and confirm its log reports successful machine registration.
4. Run a short Claude Code session under the same OS user, then allow a sync
   cycle to complete.
5. Check the dashboard's machine selector, **Sync**, and **Sessions** pages.
6. Sign in as a non-admin test user and confirm only the intended machines are
   visible.

`/api/health`, static files, authentication routes, and unattended agent routes
do not use interactive browser login. Agent writes have separate token checks;
initial enrollment still needs appropriate network access controls.

## Configuration reference

Direct Python execution loads `.env` from the repository root without overriding
variables already set in the process environment. Docker loads variables through
`--env-file` or Compose `env_file`; the file itself is not baked into the image.
Restart/recreate the server after changing environment configuration.

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_SESSION_SECRET` | Random per-process value if empty | Session signing and provider-secret encryption; set a persistent value |
| `DASHBOARD_ADMIN_EMAILS` | Empty | Comma-separated administrator emails inserted at startup |
| `DASHBOARD_SUPER_USER_EMAILS` | Empty | Comma-separated restricted-feature users inserted at startup |
| `DASHBOARD_AUTH_ENABLED` | Unset: database-driven | `true` forces login; `false` bypasses it; normally leave unset |
| `DASHBOARD_HOST` | `127.0.0.1` in Python | Bind address when running `python -m src.api.main` |
| `DASHBOARD_PORT` | `8765` | Bind port when running `python -m src.api.main` |
| `DASHBOARD_DATA_DIR` | `~/.claude-dashboard` | Data directory; `/app/data` in Docker |
| `DASHBOARD_DB_PATH` | `<data-dir>/claude.db` | SQLite database path; `/app/data/claude.db` in Docker |
| `ROOT_PATH` | Empty | External proxy path prefix, such as `/analytics` |
| `AZURE_DEVOPS_ORG_URL` | Empty | Optional organization URL |
| `AZURE_DEVOPS_PAT` | Empty | Optional Azure DevOps personal access token |
| `AZURE_DEVOPS_API_VERSION` | `7.1` | Azure DevOps REST API version |
| `AZURE_AD_CLIENT_ID`, `AZURE_AD_CLIENT_SECRET`, `AZURE_AD_TENANT_ID` | Empty | Legacy one-time migration into a stored `azure-ad` provider |

Use absolute paths for custom data/database locations and ensure the service
user can write them. Create the parent directory yourself if `DASHBOARD_DB_PATH`
is outside `DASHBOARD_DATA_DIR`.

The Docker entrypoint and `start_server.sh` explicitly pass Uvicorn host/port
arguments; those take precedence over the application bind settings. Change
Docker's published host port to change how clients reach the container, for
example `127.0.0.1:9000:8765`. Keep the internal port at 8765 unless you also
update the entrypoint and health check.

Never set `DASHBOARD_AUTH_ENABLED=false` on a shared deployment as a login fix.
Forcing it to `true` before creating a usable provider/account can lock you out.

## Optional Azure DevOps integration

This feature is independent of Microsoft login. You can use local accounts,
Google, or another login provider while connecting Azure DevOps, or omit Azure
DevOps entirely.

Create a PAT with **Code (Read)** permissions and access to the intended projects.
Project discovery may also require **Project and Team (Read)** permissions under
your organization's policies. Supply the following through your deployment's
private environment/secret mechanism:

```dotenv
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-org
AZURE_DEVOPS_PAT=replace-with-your-private-token
AZURE_DEVOPS_API_VERSION=7.1
```

Restart/recreate the server, open **Admin -> Azure DevOps**, and trigger a sync.
The integration discovers projects and repositories accessible to the token;
manual working-directory-to-repository mappings are no longer required.

Sessions are linked to pull requests using identity and time-window matching,
not commit-hash proof. The PAT is not entered through the UI or stored in the
database. Do not commit it. Leave both the organization URL and PAT unset to
disable the integration.

## Data and privacy

Depending on the source and enabled collectors, stored data can include:

- Machine names, OS details, reported user identity, and working directories.
- Sessions, timestamps, duration, model names, token counts, and estimated costs.
- Conversation messages/prompts, tool activity, and before/after code snippets.
- File activity, process/network counters, and development metrics.
- Azure DevOps pull-request/commit information when that integration is enabled.

Treat the database, logs, backups, and exports as sensitive. Redaction helpers
exist, but do not assume all collection paths remove every secret or code fragment.
Review collection behavior against your organization's requirements.

This is **self-hosted**, not universally offline:

| Component | Network behavior |
|---|---|
| Client agents | Send data and check for updates against your configured dashboard |
| Ask and tone analysis | Use local logic/dictionaries rather than an external LLM |
| SSO | Contacts the identity provider you configure |
| Azure DevOps | Contacts your configured organization when used |
| Browser UI | Loads Google Fonts/icons and Chart.js from public CDNs |
| Installation | Downloads Python packages and/or container base images |

For an air-gapped deployment, you must also vendor the external frontend assets
and provide offline dependency/image sources; the repository is not an
out-of-the-box air-gapped distribution.

## Logs and service management

| Installation | Where to inspect |
|---|---|
| Foreground Python server | Terminal stdout/stderr |
| `start.sh` server | `~/.claude-dashboard/logs/dashboard.log` |
| Docker CLI server | `docker logs --tail 100 whirlpoolbee` |
| Compose server | `docker compose -f compose.local.yaml logs --tail 100 dashboard` |
| macOS/Linux agent | `~/.claude-dashboard-agent/install.log`, `agent.log`, `agent.error.log` |
| Windows agent | The same filenames under `%USERPROFILE%\.claude-dashboard-agent` |

Agent status commands:

```bash
# macOS
launchctl list | grep com.claude.dashboard.agent

# Linux
systemctl --user status claude-dashboard-agent
journalctl --user -u claude-dashboard-agent --since "15 minutes ago"
```

```powershell
# Windows
schtasks /query /tn ClaudeDashboardAgent /v /fo list
Get-Content "$env:USERPROFILE\.claude-dashboard-agent\agent.log" -Tail 100
```

Stop/start the server using the method used to launch it:

```bash
# Docker CLI
docker stop whirlpoolbee
docker start whirlpoolbee

# Compose
docker compose -f compose.local.yaml stop
docker compose -f compose.local.yaml start
```

`docker restart` and Compose `restart` do not reload changed container
environment values. Recreate the container for configuration changes.

## Backups, upgrades, and recovery

### Back up before changing versions or authentication

Preserve the SQLite database **and** the original `DASHBOARD_SESSION_SECRET`.
Also retain deployment configuration and securely stored integration credentials.
A database backup without its encryption key cannot recover saved provider secrets.

For the Python quickstart above, use SQLite's backup operation rather than
copying a live database file:

```bash
mkdir -p backups
chmod 700 backups
sqlite3 "$HOME/.whirlpoolbee-server/claude.db" \
  ".backup 'backups/claude-backup.db'"
chmod 600 backups/claude-backup.db
```

Use a unique destination for each backup and substitute your actual database path.
The `sqlite3` command-line tool is a separate OS prerequisite for this example.
Back up `.env` through a secure secrets-management process, not a public archive.

For Docker, use SQLite's backup API inside the container or stop the dashboard
before taking a consistent backup of its named volume. Include any SQLite
journal/WAL files if performing a filesystem-level backup of a stopped deployment.
Store backups outside the container/volume being backed up.

### Upgrade

For a Python installation, back up first, obtain the desired release, reinstall
its `requirements.txt` into the virtual environment, and restart the server.
Database initialization applies the repository's migrations on startup.

For Compose:

```bash
docker compose -f compose.local.yaml up -d --build
```

For Docker CLI, build the new image, stop and remove only the old application
container, then repeat the original `docker run` command with the **same**
named volume, `.env`, and schema mount. Removing a container is not the same as
removing its named data volume.

Do not use `docker compose down -v` unless you intend to delete stored data.
Do not assume downgrading code reverses database migrations; restore a matching
backup when rolling back.

### Restore or move to another server

Stop the destination application, restore the database to its configured path,
restore the original session secret, verify ownership/write permissions, and
start the application. Recheck login, health, agent connectivity, and sync status.

Resetting `.env` does **not** clear stored users, administrator entries, or login
providers. Changing its session secret invalidates sessions and makes existing
encrypted provider secrets unreadable until re-entered. Use a fresh database and
fresh credentials for a different organization.

### Agent updates and forks

Agents can fetch updates from their configured dashboard, but accept them only
when the Ed25519 signature matches the public key embedded in the installed
agent. Installing `cryptography` is required for verification; missing verification
support causes updates to be refused.

If distributing a modified agent, maintain your own signing key securely, embed
the corresponding public key in your agent, and use `scripts/sign_agent.py` to
sign the served `src/web/static/client_agent.py`. The script synchronizes the
root copy. Keep the agent version, served source, and signature consistent.
Existing agents do not automatically trust a different signing key; plan initial
installation/key migration separately. Never commit a private signing key.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Server will not start | Use the project virtual environment; inspect the full traceback; verify dependencies and directory permissions |
| `no such table: auth_providers` or `local_users` in Docker | Ensure `schema_v4.sql` is mounted as shown above, then recreate the container |
| Port 8765 already in use | Stop only the known conflicting instance or choose a different host port; update client/proxy URLs |
| Health is good but dashboard is empty | The server is not a collector; check agent registration, source discovery, and the Sync page |
| Agent reports no data | Run it as the Claude Code user; create a session; verify JSONL paths and permissions |
| JSONL sessions are ignored on the server host | Check whether the agent selected `~/.claude-dashboard/claude.db`; keep the server destination separate from source databases |
| Agent cannot reach server | Check DNS, TLS trust, firewall, URL prefix, and use the server's address rather than client `localhost` |
| Agent registration returns `401`/`403` after reinstall | Its `.agent_token` may have been deleted; restore the private token or coordinate server-side recovery |
| Login loops or state mismatch | Use HTTPS, preserve cookies, keep the session secret stable, and check forwarded headers/callback URLs |
| Signed-in user receives `403` | Confirm administrator allow-list or machine-identity mapping; a login account alone grants no fleet-wide access |
| Saved provider secret cannot be decrypted | Restore the original session secret or re-enter the provider secret using a working administrator login |
| Windows opens the Microsoft Store for Python | Install real Python and correct conflicting App Execution Aliases/PATH entries |
| Agent exits immediately or appears inactive | Inspect `install.log` and `agent.error.log`; confirm its task/service actually started |
| Charts/icons are missing | Check browser access to the external frontend CDNs or supply locally hosted assets |
| Azure DevOps sync fails | Check token expiry/scopes, project access, organization URL, and outbound connectivity |

Do not troubleshoot by publishing an unauthenticated server or disabling TLS
verification. Inspect logs locally and redact credentials and conversation
content before sharing them.

## Development and project layout

```text
src/
  api/main.py              FastAPI routes, authentication middleware, startup
  database.py              SQLite access, schema initialization, migrations
  collectors/              Optional local metrics collectors
  integrations/            Azure DevOps client and sync
  parsers/                 Session and shell-history parsers
  utils/                   Configuration, authentication, encryption, analysis
  web/templates/           Jinja2 pages
  web/static/              CSS, JavaScript, and served client installers/agent
client_agent.py            Standalone agent source
client-install.sh          macOS/Linux/Git Bash agent installer
client-install.ps1         Native Windows agent installer
schema*.sql               Database schema and extensions
scripts/                  Collector entrypoints and agent signing utility
tests/                    Automated tests
.env.example              Non-secret configuration template
```

Install dependencies into the virtual environment as described earlier, then run:

```bash
./venv/bin/python -m pytest tests/
```

Use disposable configuration/data for development, not a production database.
PowerShell installer tests are skipped when `pwsh` is unavailable.

The server exposes generated API documentation at `/docs` and an OpenAPI schema
at `/openapi.json`; access follows the application's authentication middleware.

Additional documentation: [Deployment](DEPLOYMENT.md),
[multi-machine setup](MULTI_MACHINE_SETUP.md), [testing](TESTING.md), and
[troubleshooting](docs/TROUBLESHOOTING.md). Some older guides describe legacy
collectors; prefer the setup paths and current-behavior notes in this README.

Contributions to collectors, parsers, the dashboard, tests, and documentation are
welcome. Keep examples organization-neutral and never include real credentials,
collected session data, or private infrastructure identifiers.

## License

[MIT License](LICENSE).
