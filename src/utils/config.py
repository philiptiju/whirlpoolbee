"""Configuration management for WhirlpoolBee."""

import os
from pathlib import Path
from typing import Any

try:
    # Optional: load a local .env file (e.g. AZURE_AD_* vars) if present.
    # Never overrides real environment variables (e.g. those set in k8s/docker).
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
except ImportError:
    pass


class Config:
    """Application configuration."""

    # Application
    APP_NAME = "claude-local-dashboard"

    # Paths — respect env vars for Docker deployment
    HOME_DIR = Path.home()
    DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", str(HOME_DIR / ".claude-dashboard")))
    DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", str(DATA_DIR / "claude.db")))
    LOG_DIR = DATA_DIR / "logs"

    # Web server
    HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
    RELOAD = False

    # Claude data locations
    CLAUDE_LOCATIONS = [
        HOME_DIR / ".claude",
        HOME_DIR / ".config" / "claude",
        HOME_DIR / "Library" / "Application Support" / "Claude",
    ]

    # Windows JSONL session locations
    # On Windows, Claude Code stores sessions as JSONL at:
    #   %USERPROFILE%\.claude\projects\<project-slug>\<uuid>.jsonl
    WINDOWS_JSONL_LOCATIONS = [
        HOME_DIR / ".claude" / "projects",
    ]

    # Shell history files
    SHELL_HISTORY_FILES = [
        HOME_DIR / ".zsh_history",
        HOME_DIR / ".bash_history",
    ]

    # Collection intervals (seconds)
    PROCESS_METRICS_INTERVAL = 60  # 1 minute
    NETWORK_METRICS_INTERVAL = 300  # 5 minutes
    FILESYSTEM_SCAN_INTERVAL = 900  # 15 minutes
    DAILY_ROLLUP_TIME = "00:00"  # midnight

    # Legacy Azure AD env vars -- read once at startup to auto-migrate into a
    # DB-backed auth_providers row (see Database._migrate_azure_ad_env_to_auth_provider)
    # the first time the app runs with them set, then ignored. Login providers
    # (Azure AD, Google, GitHub, generic OIDC/OAuth2) are now configured
    # entirely through the Admin -> Login Providers UI, stored in the
    # database -- not environment variables -- so they can be added/edited/
    # removed without a redeploy.
    AZURE_AD_CLIENT_ID = os.environ.get("AZURE_AD_CLIENT_ID", "")
    AZURE_AD_CLIENT_SECRET = os.environ.get("AZURE_AD_CLIENT_SECRET", "")
    AZURE_AD_TENANT_ID = os.environ.get("AZURE_AD_TENANT_ID", "")

    # Explicit override: "true"/"false". When unset (the default), whether
    # login is required is decided dynamically from the database (True as
    # soon as at least one login provider or local account is configured via
    # the admin UI) -- see Database.is_auth_required() and
    # request.state.auth_enabled in main.py. Set this only to force auth on
    # or off regardless of what's configured (e.g. to temporarily disable
    # login for local debugging without touching any provider rows).
    _AUTH_OVERRIDE_RAW = os.environ.get("DASHBOARD_AUTH_ENABLED")
    AUTH_OVERRIDE = (
        _AUTH_OVERRIDE_RAW.strip().lower() in ("1", "true", "yes", "on")
        if _AUTH_OVERRIDE_RAW is not None else None
    )

    # Signs the session cookie. Set explicitly in production (e.g. via a k8s
    # secret) so sessions survive pod/process restarts; falls back to a
    # random per-process key otherwise (users get logged out on restart).
    SESSION_SECRET_KEY = os.environ.get("DASHBOARD_SESSION_SECRET") or os.urandom(32).hex()

    # Azure DevOps integration (links sessions to PRs/commits for "AI-assisted
    # PR" stats). Create a Personal Access Token with "Code (Read)" scope at
    # <org-url>/_usersSettings/tokens and set it as an environment variable —
    # never store it in the database, a config file, or source control.
    # Auth uses HTTP Basic with an empty username and the PAT as the password,
    # per Azure DevOps REST API convention.
    AZURE_DEVOPS_ORG_URL = os.environ.get("AZURE_DEVOPS_ORG_URL", "").rstrip("/")
    AZURE_DEVOPS_PAT = os.environ.get("AZURE_DEVOPS_PAT", "")
    AZURE_DEVOPS_API_VERSION = os.environ.get("AZURE_DEVOPS_API_VERSION", "7.1")
    AZURE_DEVOPS_ENABLED = bool(AZURE_DEVOPS_ORG_URL and AZURE_DEVOPS_PAT)

    @classmethod
    def ensure_directories(cls):
        """Ensure required directories exist.

        Tolerates PermissionError so a container running as non-root
        with a pre-mounted volume (e.g. k8s PVC before fsGroup chown
        propagates, or a read-only mount) doesn't crash the app if the
        directories already exist and are usable.
        """
        for path in (cls.DATA_DIR, cls.LOG_DIR):
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except PermissionError:
                if not path.exists():
                    raise

    @classmethod
    def get_env(cls, key: str, default: Any = None) -> Any:
        """Get environment variable with fallback."""
        return os.environ.get(key, default)
