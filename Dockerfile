# ── Claude Local Dashboard ───────────────────────────────
# Multi-machine analytics dashboard for Claude Code usage
#
# Build:  docker build -t claude-local-dashboard .
# Run:    docker run -d -p 8765:8765 -v claude-data:/app/data \
#           --env-file .env --name claude-dashboard claude-local-dashboard
#
# --env-file .env loads AZURE_AD_CLIENT_ID / AZURE_AD_CLIENT_SECRET /
# AZURE_AD_TENANT_ID / DASHBOARD_SESSION_SECRET (see .env.example) as
# container environment variables at runtime — the file itself is never
# copied into the image (see .dockerignore). Omit the flag / leave the
# file empty to keep the dashboard open (no SSO).
#
# The SQLite database is stored in /app/data inside the container.
# Mount a volume to persist data across container restarts.

# ── Stage 1: Builder ─────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies for compiling Python packages (psutil, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="Claude Local Dashboard"
LABEL description="Multi-machine analytics dashboard for Claude Code usage"
LABEL version="1.0.0"

# Security: run as non-root with fixed UID/GID (matched by k8s
# podSecurityContext.fsGroup so mounted PVC volumes are writable)
RUN groupadd -r -g 1000 dashboard && useradd -r -u 1000 -g dashboard -m dashboard

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY schema.sql .
COPY schema_extensions.sql .
COPY schema_v3.sql .
COPY src/ src/
COPY client_agent.py .
COPY VERSION .

# Create data directory for SQLite DB and logs
RUN mkdir -p /app/data/logs && chown -R dashboard:dashboard /app /app/data

# Environment configuration
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DASHBOARD_DATA_DIR=/app/data \
    DASHBOARD_DB_PATH=/app/data/claude.db \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8765 \
    ROOT_PATH=""

# Switch to non-root user
USER dashboard

EXPOSE 8765

# Health check — hits the /api/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

# Start the FastAPI server
CMD ["python", "-m", "uvicorn", "src.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--log-level", "info", \
     "--no-access-log"]
