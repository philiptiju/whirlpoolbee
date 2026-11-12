# Deployment Guide

This project ships as a single Docker image. There is no required
orchestration platform — run it with plain `docker run`, with
`docker-compose`, or behind whatever reverse proxy / scheduler your
organization already uses.

## 1. Build the image

```bash
docker build -t claude-local-dashboard .
```

## 2. Run it

### Option A: `docker run`

```bash
docker run -d \
  -p 8765:8765 \
  -v claude-data:/app/data \
  --env-file .env \
  --name claude-dashboard \
  claude-local-dashboard
```

- `-v claude-data:/app/data` persists the SQLite database and logs across
  restarts. Omit it only for throwaway/test runs.
- `--env-file .env` loads optional configuration (session secret, Azure
  DevOps integration, etc. — see `.env.example`). Login providers
  (Google/GitHub/Azure AD/generic OIDC) are configured from the UI at
  Admin -> Login Providers, not via env vars — see README "Login & SSO".
  Omit the flag or leave `.env` empty to run fully open (no login) until
  you add a provider.

### Option B: docker-compose

```bash
docker compose up -d
```

See `docker-compose.yml` — it builds the image, maps port 8765, mounts a
named volume for `/app/data`, and loads `.env` the same way as Option A.

## 3. Verify

```bash
curl http://localhost:8765/api/health
```

## Configuration

All runtime configuration is done via environment variables — nothing is
hardcoded to a specific company, cloud, or network. See `.env.example` for
the full list, including:

| Variable | Purpose |
|---|---|
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Bind address/port (defaults `0.0.0.0:8765` in the container) |
| `DASHBOARD_DATA_DIR` / `DASHBOARD_DB_PATH` | Where the SQLite database and logs live |
| `ROOT_PATH` | Set this if the dashboard is served under a URL prefix (e.g. behind a reverse proxy at `/some/path`) |
| `AZURE_AD_CLIENT_ID` / `AZURE_AD_CLIENT_SECRET` / `AZURE_AD_TENANT_ID` | One-time migration only for pre-existing deployments; new ones should configure login providers from the UI instead — see README "Login & SSO" |
| `DASHBOARD_SESSION_SECRET` | Session cookie signing key + login-provider secret encryption key (required once any login provider is configured) |
| `AZURE_DEVOPS_ORG_URL` / `AZURE_DEVOPS_PAT` | Optional Azure DevOps integration (links sessions to PRs/commits) |

## Putting it behind a reverse proxy / ingress

If you front the container with nginx, Traefik, an Ingress controller, or
similar:
- Forward `X-Forwarded-Proto` so the app knows it's served over HTTPS
  (needed for secure session cookies once a login provider is configured).
- Set `ROOT_PATH` to match the path prefix you're routing on, if any.
- Point health checks at `/api/health`.

## Persisting data

The container stores everything (SQLite DB + logs) under `/app/data`. Mount
a volume or bind-mount at that path to persist data across restarts/upgrades
and to back it up.

## Upgrading

```bash
docker build -t claude-local-dashboard .
docker stop claude-dashboard && docker rm claude-dashboard
docker run -d -p 8765:8765 -v claude-data:/app/data --env-file .env \
  --name claude-dashboard claude-local-dashboard
```

(With docker-compose: `docker compose up -d --build`.)

Your data persists in the `claude-data` volume across upgrades.

## Running without Docker

See `install.sh` / `QUICKSTART.md` for running the dashboard directly with
Python (useful for local single-user use without containers).
