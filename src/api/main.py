"""FastAPI application for WhirlpoolBee."""

import re
import sqlite3
import hashlib
import asyncio
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, Dict
from urllib.parse import parse_qsl, urlencode
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.config import Config
from src.database import Database
from src.utils import auth as auth_utils
from src.utils.crypto import encrypt_secret
from src.utils.passwords import hash_password, verify_password
from src.utils.nlq import parse_query, METRIC_LABELS
from src.integrations.azure_devops import sync_azure_devops, AzureDevOpsClient, AzureDevOpsError, vote_label

# Setup templates and static files
templates_dir = Path(__file__).parent.parent / "web" / "templates"
static_dir = Path(__file__).parent.parent / "web" / "static"

templates = Jinja2Templates(directory=str(templates_dir))
# Content hashes keep cached assets in sync with templates across deployments,
# including UI-only releases that do not change the client-agent version.
templates.env.globals["static_versions"] = {
    path.relative_to(static_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    for directory in ("css", "js")
    for path in (static_dir / directory).rglob("*")
    if path.is_file()
}


def format_compact_number(value):
    """Format a number using K/M/B suffixes for thousands/millions/billions."""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return value

    sign = "-" if num < 0 else ""
    num = abs(num)

    tiers = [(1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for i, (threshold, suffix) in enumerate(tiers):
        if num >= threshold:
            scaled = round(num / threshold, 2)
            if scaled >= 1000 and i > 0:
                # Rounding pushed into the next larger unit (e.g. 999999999 -> 1B).
                threshold, suffix = tiers[i - 1]
                scaled = round(num / threshold, 2)
            formatted = f"{scaled:.2f}".rstrip("0").rstrip(".")
            return f"{sign}{formatted}{suffix}"
    return f"{sign}{num:,.0f}"


templates.env.filters["compact_number"] = format_compact_number
templates.env.filters["display_name"] = Database._display_name


def get_agent_version() -> str:
    """Read the client-agent version straight off the static/client_agent.py
    file being served (single source of truth for "what version is
    current"). Used only by /api/agent/version (auto-update check) --
    this must NOT be bumped on every dashboard release, since doing so
    forces every installed agent to auto-update/restart (causing the
    short data gaps users have seen), even when only the web dashboard
    changed and the agent itself did not."""
    try:
        agent_path = static_dir / "client_agent.py"
        text = agent_path.read_text(encoding="utf-8")
        match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


_VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"


def get_app_version() -> str:
    """Read the dashboard/webapi version from the repo-root VERSION file --
    the single source of truth bumped by push-to-acr.sh on every release.
    This is intentionally separate from get_agent_version(): the footer
    should reflect what was actually just deployed to this webapi pod,
    not the unrelated collector-agent version."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


templates.env.globals["app_version"] = get_app_version
templates.env.globals["agent_version"] = get_agent_version

# Initialize database
Config.ensure_directories()
db = Database(str(Config.DB_PATH))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events."""
    # Startup
    db.initialize()
    yield
    # Shutdown (if needed)


# Initialize FastAPI with lifespan
# root_path tells FastAPI it's behind a proxy at this path (for correct URL generation)
import os
ROOT_PATH = os.getenv("ROOT_PATH", "")


def _default_next() -> str:
    """The app's own root (ROOT_PATH + "/"), never the bare domain root —
    nginx-ingress serves this app under a sub-path, and a bare "/" redirect
    falls through to whatever else owns "/" on that host."""
    return f"{ROOT_PATH}/" if ROOT_PATH else "/"

# A session left open (idle terminal, laptop asleep, etc.) can accumulate a
# huge wall-clock `duration_seconds` that doesn't reflect real "usage" time.
# Cap any single session's contribution to usage-time aggregates so one stale
# session can't blow up daily/monthly totals (e.g. showing "73h" for one day).
MAX_SESSION_DURATION_SECONDS = 8 * 3600  # 8 hours

app = FastAPI(
    title="WhirlpoolBee",
    description="Fully local usage analytics for Claude Code",
    version=get_app_version(),
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Store in app state
app.state.db = db
app.state.templates = templates

# Add context processor to inject root_path into all templates
@app.middleware("http")
async def add_root_path_to_request(request: Request, call_next):
    """Add root_path to request state for template access."""
    request.state.root_path = ROOT_PATH
    response = await call_next(request)
    return response


@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    """Require an authenticated session (via any enabled login provider or
    local account) for every page and API route, then (once signed in)
    restrict non-admin users to only the machine(s) registered to them --
    admin_users grants unrestricted access, matching this app's original
    (pre-scoping) behavior; everyone else is forced onto their own
    machine_id(s) regardless of what's requested, and gets a "no machine
    registered" page if none match. Left as a no-op (open access,
    unrestricted machines) when no login provider/local account is
    configured at all, so local development is unaffected. See
    src/utils/auth.py for the list of always-public paths (auth flow,
    static assets, health probe, and the unattended machine-agent ingestion
    endpoints)."""
    request.state.auth_enabled = (
        Config.AUTH_OVERRIDE if Config.AUTH_OVERRIDE is not None
        else await app.state.db.is_auth_required()
    )
    if not request.state.auth_enabled or auth_utils.is_public_path(request.url.path, ROOT_PATH):
        if not request.state.auth_enabled:
            # No identity to check admin status against locally/in dev -
            # match the rest of the app's open-access fallback rather than
            # hiding admin-only/super-user-only features (e.g. the
            # Leaderboard/Conversation Tone nav links) for everyone.
            request.state.is_admin = True
            request.state.is_super_user = True
        return await call_next(request)

    path = request.url.path
    is_api = path.startswith(f"{ROOT_PATH}/api/") or (not ROOT_PATH and path.startswith("/api/"))

    user = auth_utils.current_user(request)
    if user is None:
        if is_api:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # request.url.path never includes ROOT_PATH: nginx-ingress strips the
        # /app/claude/v9 prefix (rewrite-target) before forwarding to this
        # pod. next_path is handed back to the browser (post-login redirect
        # target), so it must be re-prefixed with ROOT_PATH or the browser
        # ends up at the bare domain root instead of the app's sub-path.
        next_path = f"{ROOT_PATH}{path}"
        if request.url.query:
            next_path += f"?{request.url.query}"
        return RedirectResponse(url=f"{ROOT_PATH}/auth/login?next={next_path}")

    email = (user.get("email") or "").strip()
    is_admin = await app.state.db.is_admin_user(email) if email else False
    request.state.is_admin = is_admin
    # Independent of is_admin - see super_users table comment in database.py.
    request.state.is_super_user = await app.state.db.is_super_user(email) if email else False
    if is_admin:
        request.state.allowed_machine_ids = None  # None == unrestricted
        return await call_next(request)

    machine_ids = await app.state.db.get_machine_ids_for_user(email)
    request.state.allowed_machine_ids = machine_ids
    if not machine_ids:
        if is_api:
            return JSONResponse(
                {"detail": "No machine is registered to your account yet."},
                status_code=403,
            )
        return _better_luck_page()

    # Force every machine_id-scoped route onto the caller's own machine(s),
    # overriding whatever (if anything) was requested in the query string --
    # this is what stops a non-admin from viewing someone else's data by
    # editing the URL. Database._machine_scope_clause() already accepts a
    # comma-separated list, so every existing machine_id-aware route/query
    # picks this up with no per-route changes needed.
    query_pairs = [
        (k, v) for k, v in parse_qsl(request.scope["query_string"].decode("latin-1"), keep_blank_values=True)
        if k != "machine_id"
    ]
    query_pairs.append(("machine_id", ",".join(machine_ids)))
    request.scope["query_string"] = urlencode(query_pairs).encode("latin-1")

    return await call_next(request)


# SessionMiddleware must be added after the middleware above so it ends up
# outermost (Starlette wraps the most-recently-added middleware around the
# earlier ones), guaranteeing request.session is populated before either of
# the middleware functions above run.
from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(
    SessionMiddleware,
    secret_key=Config.SESSION_SECRET_KEY,
    session_cookie="whirlpoolbee_session",
    # response_mode=form_post makes Azure AD POST the auth code back to us
    # from login.microsoftonline.com (a cross-site context). SameSite=Lax
    # cookies are not sent on cross-site POSTs, so the session (holding the
    # MSAL flow state) would be lost on every login. SameSite=None restores
    # it; https_only=True is required by browsers whenever SameSite=None is
    # used (the site is always served over https in every deployed env).
    same_site="none",
    https_only=True,
)


@app.get("/auth/login")
async def auth_login(request: Request, next: str = "", error: str = ""):
    """Show the login chooser page (every enabled provider as a button, plus
    a username/password form if any local accounts exist) -- or, when no
    login is configured at all, fall straight through to `next`."""
    # Falls back to the app's own root (ROOT_PATH + "/"), not the bare
    # domain root, since a reverse proxy may serve this app under a
    # sub-path. `next` must default to "" (not "/"): a bare "/" would itself
    # pass the startswith("/") check below and get used verbatim, sending
    # users to the un-prefixed domain root (which a reverse proxy may route
    # to a totally different app) whenever /auth/login is hit without an
    # explicit ?next= query param (direct nav, bookmark, expired session,
    # etc.).
    default_next = _default_next()
    safe_next = next if next.startswith("/") and not next.startswith("//") else default_next
    if not request.state.auth_enabled:
        return RedirectResponse(url=safe_next)

    request.session["next"] = safe_next
    providers = [p for p in await app.state.db.list_auth_providers() if p["enabled"]]
    local_users = await app.state.db.list_local_users()
    has_local_accounts = any(u["enabled"] for u in local_users)
    return app.state.templates.TemplateResponse(
        "auth_login.html",
        {
            "request": request,
            "providers": providers,
            "has_local_accounts": has_local_accounts,
            "error": error,
        },
    )


@app.post("/auth/login/local")
async def auth_login_local(request: Request):
    """Verify a username/password submission against an admin-created
    local account (see src/utils/passwords.py)."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    local_user = await app.state.db.get_local_user_by_email(email) if email else None
    if (
        not local_user
        or not local_user["enabled"]
        or not verify_password(password, local_user["password_hash"])
    ):
        return RedirectResponse(
            url=f"{ROOT_PATH}/auth/login?error=Invalid+email+or+password",
            status_code=302,
        )
    await app.state.db.touch_local_user_login(local_user["id"])
    request.session["auth_provider_key"] = "local"
    return await _complete_login(request, {
        "name": local_user["name"] or email,
        "email": email,
        "oid": f"local:{local_user['id']}",
    })


@app.get("/auth/login/{provider_key}")
async def auth_login_provider(request: Request, provider_key: str):
    """Kick off a specific provider's sign-in flow."""
    if not request.state.auth_enabled:
        return RedirectResponse(url=_default_next())
    provider = await app.state.db.get_auth_provider(provider_key)
    if not provider or not provider["enabled"]:
        return _auth_error_page("Sign-in unavailable", "This login provider is not configured or has been disabled.")
    try:
        if provider["provider_type"] == "azure_ad":
            auth_url = auth_utils.build_azure_auth_code_url(request, provider)
        else:
            auth_url = auth_utils.build_generic_auth_code_url(request, provider)
    except Exception as exc:
        return _auth_error_page("Sign-in is misconfigured", str(exc))
    return RedirectResponse(url=auth_url)


def _auth_error_page(title: str, message: str) -> HTMLResponse:
    """Render a full HTML document for auth failures, returned with a 200
    status. Chrome (and some other browsers) silently replace small HTML
    bodies sent with a 4xx/5xx status on a top-level navigation with their
    own generic "This page isn't working / HTTP ERROR 401" interstitial,
    hiding our actual message from the user. Since this is a human-facing
    page (not an API response consumed by code), 200 avoids that override
    while the wording still makes the failure clear."""
    return HTMLResponse(
        "<!DOCTYPE html><html><head><title>Sign-in error</title></head>"
        "<body style=\"font-family: sans-serif; max-width: 640px; margin: 80px auto; text-align: center;\">"
        f"<h2>{title}</h2><p>{message}</p>"
        f"<p><a href=\"{ROOT_PATH}/auth/login\">Try again</a></p>"
        "</body></html>",
        status_code=200,
    )


def _better_luck_page() -> HTMLResponse:
    """Shown (200, for the same Chrome-interstitial-avoidance reason as
    _auth_error_page) to a signed-in, non-admin user with no machine of
    their own registered yet (see Database.get_machine_ids_for_user)."""
    return HTMLResponse(
        "<!DOCTYPE html><html><head><title>No machine found</title></head>"
        "<body style=\"font-family: sans-serif; max-width: 640px; margin: 80px auto; text-align: center;\">"
        "<h2>Better luck next time!</h2>"
        "<p>We couldn't find a machine registered to your account. "
        "Install and run client_agent.py on your machine, or contact an admin "
        "if you believe this is a mistake.</p>"
        "</body></html>",
        status_code=200,
    )


def _require_admin(request: Request):
    """Guard for /api/admin/* endpoints. Returns a JSONResponse to send back
    immediately if the caller isn't allowed, or None if they are. When no
    login is configured there's no signed-in identity to check admin status
    against, so this matches the rest of the app's open-access fallback
    rather than locking everyone out."""
    if not request.state.auth_enabled:
        return None
    if not getattr(request.state, "is_admin", False):
        return JSONResponse({"detail": "Admin access required"}, status_code=403)
    return None


async def _require_agent_token(request: Request, machine_id: Optional[str]):
    """Guard for every /api/client/* write (these are necessarily exempt
    from enforce_auth -- see PUBLIC_PATH_PREFIXES in src/utils/auth.py --
    since unattended client_agent.py instances can't do interactive login).
    Without this, anyone who can reach the endpoint could spoof/tamper with
    any machine_id's data (CWE-306). Returns a JSONResponse to send back
    immediately if the write should be rejected, or None if it's allowed;
    `request.state.issued_agent_token` is set when a brand-new/legacy
    machine needs to be handed back a freshly-issued token in the response
    body."""
    if not machine_id:
        return JSONResponse({"status": "error", "message": "machine_id required"}, status_code=400)
    provided = request.headers.get("x-agent-token")
    allowed, issued = await app.state.db.verify_or_issue_agent_token(machine_id, provided)
    if not allowed:
        return JSONResponse(
            {"detail": "Invalid or missing agent token for this machine_id"},
            status_code=403,
        )
    request.state.issued_agent_token = issued
    return None


async def _deny_foreign_session(session_id: Optional[str], machine_id: Optional[str]):
    """Reject agent writes that target a session_id already owned by a
    different machine. Transcript/token views look up messages and
    token_usage by session_id alone, so without this a machine could inject
    content or cost into another machine's session."""
    if not session_id:
        return JSONResponse({"status": "error", "message": "session_id required"}, status_code=400)
    async with app.state.db.get_connection() as conn:
        async with conn.execute(
            "SELECT DISTINCT machine_id FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            owners = {row[0] for row in await cursor.fetchall()}
    if owners and machine_id not in owners:
        return JSONResponse(
            {"detail": "session_id belongs to another machine"}, status_code=403
        )
    return None


def _check_machine_scope(request: Request, machine_id: Optional[str]):
    """Guard for routes that take a machine/user scope as a *path* param
    instead of the `machine_id` query-string param enforce_auth already
    rewrites for non-admins -- path params bypass that rewrite entirely
    (CWE-639), so routes using one must call this explicitly. `machine_id`
    may be a single id or the comma-separated list this app's convention
    uses for a Team/Group scope. Returns a JSONResponse to reject if the
    caller (a non-admin) is requesting a machine_id outside their own
    allowed set, or None if it's fine."""
    allowed = getattr(request.state, "allowed_machine_ids", None)
    if allowed is None:
        return None  # admin, or no login configured -- unrestricted
    requested = {m.strip() for m in (machine_id or "").split(",") if m.strip()}
    if not requested or not requested.issubset(set(allowed)):
        return JSONResponse({"detail": "Forbidden: not your machine"}, status_code=403)
    return None


def _require_super_user(request: Request):
    """Guard for the Conversation Tone page/API - a narrower allowlist than
    admin (see super_users table in database.py). Same open-access fallback
    as _require_admin when no login is configured."""
    if not request.state.auth_enabled:
        return None
    if not getattr(request.state, "is_super_user", False):
        return JSONResponse({"detail": "Access restricted"}, status_code=403)
    return None


async def _complete_login(request: Request, user: dict):
    """Shared tail of every successful login (any provider): stash the user
    in the session and redirect to wherever they were headed."""
    request.session["user"] = user
    next_path = request.session.pop("next", None) or _default_next()
    return RedirectResponse(url=next_path, status_code=302)


@app.api_route("/auth/callback", methods=["GET", "POST"])
async def auth_callback(request: Request):
    """Handle the redirect back from Microsoft after sign-in (the Azure AD
    App registration's redirect URI points here, suffix-less, for backward
    compatibility -- see auth_utils.get_redirect_uri). Azure AD posts here
    (response_mode=form_post) rather than appending the auth code to the
    URL -- GET is kept too for compatibility if that's ever overridden."""
    if not request.state.auth_enabled:
        return RedirectResponse(url=_default_next())

    provider_key = request.session.get("auth_provider_key") or "azure-ad"
    provider = await app.state.db.get_auth_provider(provider_key)
    if not provider:
        return _auth_error_page("Sign-in unavailable", "The Microsoft/Azure AD login provider is no longer configured.")

    if request.method == "POST":
        auth_response = dict(await request.form())
    else:
        auth_response = dict(request.query_params)

    try:
        result = auth_utils.acquire_azure_token_by_auth_code(request, provider, auth_response)
    except ValueError:
        # Flow state missing/expired in the session (cookie lost, user took
        # too long, or started the flow on a different replica) — send them
        # back to sign in again instead of a raw 500.
        return _auth_error_page(
            "Sign-in session expired",
            "Please try signing in again.",
        )
    if "error" in result:
        return _auth_error_page(
            "Sign-in failed",
            f"{result.get('error')}: {result.get('error_description', '')}",
        )

    return await _complete_login(request, auth_utils.user_from_claims(result.get("id_token_claims", {})))


@app.get("/auth/callback/{provider_key}")
async def auth_callback_generic(request: Request, provider_key: str):
    """Handle the redirect back from Google/GitHub/any generic OAuth2
    provider after sign-in."""
    if not request.state.auth_enabled:
        return RedirectResponse(url=_default_next())

    provider = await app.state.db.get_auth_provider(provider_key)
    if not provider:
        return _auth_error_page("Sign-in unavailable", "This login provider is no longer configured.")

    result = await auth_utils.complete_generic_oauth2_login(request, provider, dict(request.query_params))
    if "error" in result:
        return _auth_error_page("Sign-in failed", result["error"])

    return await _complete_login(request, result)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear the local session and, for Azure AD, sign the user out of
    Microsoft too."""
    # Only look up/redirect through Azure's end-session endpoint if the
    # signed-in session actually came from Azure AD (not a local account or
    # another OAuth2 provider, neither of which has one).
    signed_in_via = request.session.get("auth_provider_key")
    azure_provider = await app.state.db.get_auth_provider("azure-ad") if signed_in_via == "azure-ad" else None
    request.session.clear()
    if not request.state.auth_enabled:
        return RedirectResponse(url=_default_next())
    if azure_provider and azure_provider["enabled"]:
        return RedirectResponse(url=auth_utils.azure_logout_url(request, azure_provider))
    return RedirectResponse(url=_default_next())


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, machine_id: str = None):
    """Dashboard overview page."""
    if machine_id and machine_id != "all":
        frag, fparams = Database._machine_scope_clause(machine_id)
        sfrag, sfparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        async with app.state.db.get_connection() as conn:
            conn.row_factory = lambda cursor, row: dict(
                zip([col[0] for col in cursor.description], row)
            )
            async with conn.execute(f"SELECT COUNT(*) as c FROM sessions WHERE 1=1{frag}", fparams) as cur:
                total_sessions = (await cur.fetchone())['c']
            async with conn.execute(f"SELECT COUNT(*) as c FROM sessions WHERE end_time IS NULL{frag}", fparams) as cur:
                active_sessions_count = (await cur.fetchone())['c']
            async with conn.execute(
                f"SELECT COALESCE(SUM(tu.total_tokens),0) as c FROM token_usage tu JOIN sessions s ON tu.session_id=s.session_id WHERE 1=1{sfrag}",
                sfparams
            ) as cur:
                total_tokens = (await cur.fetchone())['c']
            async with conn.execute(f"SELECT COUNT(*) as c FROM launches WHERE 1=1{frag}", fparams) as cur:
                total_launches = (await cur.fetchone())['c']
            async with conn.execute(f"SELECT start_time FROM sessions WHERE 1=1{frag} ORDER BY start_time DESC LIMIT 1", fparams) as cur:
                row = await cur.fetchone()
                latest_session = row['start_time'] if row else None
            async with conn.execute(
                f"SELECT COALESCE(SUM(tu.estimated_cost_usd),0) as c FROM token_usage tu JOIN sessions s ON tu.session_id=s.session_id WHERE 1=1{sfrag}",
                sfparams
            ) as cur:
                total_cost = (await cur.fetchone())['c']

        stats = {
            "total_sessions": total_sessions,
            "active_sessions": active_sessions_count,
            "total_tokens": total_tokens,
            "total_launches": total_launches,
            "total_cost": total_cost,
            "latest_session": latest_session
        }
        # Active sessions for this scope
        async with app.state.db.get_connection() as conn:
            conn.row_factory = lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))
            async with conn.execute(
                f"SELECT * FROM sessions WHERE end_time IS NULL{frag} ORDER BY start_time DESC", fparams
            ) as cur:
                active_sessions = await cur.fetchall()
    else:
        stats = await app.state.db.get_stats_overview()
        active_sessions = await app.state.db.get_active_sessions()

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "stats": stats,
            "active_sessions": active_sessions,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/usage", response_class=HTMLResponse)
async def usage_trends(request: Request, machine_id: str = None):
    """Usage trends page."""
    # Monthly costs — filtered by machine if specified
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([col[0] for col in cursor.description], row)
        )

        cost_query = """
            SELECT
                strftime('%Y-%m', tu.timestamp) as month,
                strftime('%Y', tu.timestamp) as year,
                CASE strftime('%m', tu.timestamp)
                    WHEN '01' THEN 'January' WHEN '02' THEN 'February'
                    WHEN '03' THEN 'March'   WHEN '04' THEN 'April'
                    WHEN '05' THEN 'May'     WHEN '06' THEN 'June'
                    WHEN '07' THEN 'July'    WHEN '08' THEN 'August'
                    WHEN '09' THEN 'September' WHEN '10' THEN 'October'
                    WHEN '11' THEN 'November'  WHEN '12' THEN 'December'
                END as month_name,
                COUNT(DISTINCT tu.session_id) as session_count,
                SUM(tu.input_tokens) as input_tokens,
                SUM(tu.output_tokens) as output_tokens,
                SUM(tu.total_tokens) as total_tokens,
                SUM(tu.estimated_cost_usd) as total_cost
            FROM token_usage tu
        """
        cost_params = []
        frag, cost_params = Database._machine_scope_clause(machine_id, "s.machine_id")
        if frag:
            cost_query += f" JOIN sessions s ON tu.session_id = s.session_id WHERE 1=1{frag}"
        cost_query += " GROUP BY strftime('%Y-%m', tu.timestamp) ORDER BY month DESC"
        async with conn.execute(cost_query, cost_params) as cursor:
            monthly_costs = [dict(r) for r in await cursor.fetchall()]

        # total_tokens includes cache read/creation tokens (billed separately at a
        # discount/premium rate) while input_tokens/output_tokens deliberately do
        # not — surface the difference explicitly so the UI isn't confusing.
        for m in monthly_costs:
            cache = (m.get('total_tokens') or 0) - (m.get('input_tokens') or 0) - (m.get('output_tokens') or 0)
            m['cache_tokens'] = max(cache, 0)

        # Daily usage in minutes for last 30 days
        daily_query = f"""
            SELECT
                date(start_time) as date,
                s.machine_id,
                m.machine_name,
                m.logged_in_user,
                COUNT(*) as session_count,
                COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) as total_seconds,
                ROUND(COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) / 60.0, 1) as total_minutes
            FROM sessions s
            LEFT JOIN machines m ON s.machine_id = m.machine_id
            WHERE s.start_time >= date('now', '-30 days')
        """
        daily_params = []
        frag, daily_params = Database._machine_scope_clause(machine_id, "s.machine_id")
        daily_query += frag
        daily_query += " GROUP BY date(start_time), s.machine_id ORDER BY date(start_time) DESC"
        async with conn.execute(daily_query, daily_params) as cursor:
            daily_usage = [dict(r) for r in await cursor.fetchall()]

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            "monthly_costs": monthly_costs,
            "daily_usage": daily_usage,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/projects", response_class=HTMLResponse)
async def projects(request: Request, machine_id: str = None):
    """Projects page — activity grouped by working directory."""
    repos = await app.state.db.get_top_projects(limit=20, machine_id=machine_id)

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "repositories": repos,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request, machine_id: str = None):
    """Sync page — history of client-agent sync cycles and item counts."""
    sync_log = await app.state.db.get_sync_log(limit=200, machine_id=machine_id)

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "sync.html",
        {
            "request": request,
            "sync_log": sync_log,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Install/setup help: copy-paste client agent install commands for
    Windows (native PowerShell, no Git Bash required), macOS, and Linux,
    plus troubleshooting for the most common failure modes. Open to every
    signed-in user (not admin-gated) since everyone installing the agent
    on their own machine needs it."""
    # Prefer the proxy's forwarded scheme (nginx-ingress sets this) over
    # request.url.scheme, which reflects the plain-HTTP connection between
    # the ingress and this pod, not what the browser actually used.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    server_url = f"{scheme}://{host}{ROOT_PATH}"

    return app.state.templates.TemplateResponse(
        "help.html",
        {
            "request": request,
            "server_url": server_url,
        }
    )


@app.get("/learning", response_class=HTMLResponse)
async def learning_page(request: Request, machine_id: str = None):
    """Learning & Development page."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "learning.html",
        {
            "request": request,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/optimization", response_class=HTMLResponse)
async def optimization_page(request: Request, machine_id: str = None):
    """AI Usage Optimization page."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "optimization.html",
        {
            "request": request,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, machine_id: str = None):
    """Code Analytics page."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request, machine_id: str = None):
    """Leaderboard: scoreboard of badges/achievements.

    Open to every authenticated user, but the ranked pool differs by role:
    admins get the full cross-user scoreboard (everyone, optionally
    narrowed by the Team filter); non-admins are restricted to only their
    own machine(s) -- enforced upstream by `enforce_auth`, which
    forces the `machine_id` query param onto a non-admin's own machine
    IDs for every request, so `machine_id` here already reflects the
    correctly-scoped value for both roles with no extra logic needed. For
    an admin with no machine/team selected, a single-machine selection
    doesn't narrow the ranked pool (a leaderboard is inherently a
    compare-everyone view) - it's only used to highlight "your" row.
    """
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "leaderboard.html",
        {
            "request": request,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/tone", response_class=HTMLResponse)
async def tone_analysis_page(request: Request, machine_id: str = None):
    """Tone-of-conversation analysis: per-user profanity, politeness,
    spelling, and grammar signals derived from each user's own chat
    prompts. Unlike /leaderboard, this is restricted to the super_users
    allowlist (a narrower tier than admin - see database.py) rather than
    open to everyone; non-super-users are redirected home, matching the
    existing pattern used for the admin-only /users page."""
    if request.state.auth_enabled and not getattr(request.state, "is_super_user", False):
        return RedirectResponse(url=f"{ROOT_PATH}/")

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "tone_analysis.html",
        {
            "request": request,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/users", response_class=HTMLResponse)
async def user_management_page(request: Request):
    """User/machine management: team assignment, agent version, and machine
    details for every registered machine. Restricted to admins -- everyone
    else is redirected home (the nav link is also hidden for them in
    base.html, but the route itself must enforce this too since the URL can
    be visited directly)."""
    if request.state.auth_enabled and not getattr(request.state, "is_admin", False):
        return RedirectResponse(url=f"{ROOT_PATH}/")

    return app.state.templates.TemplateResponse(
        "user_management.html",
        {"request": request}
    )


@app.get("/admin/login-providers", response_class=HTMLResponse)
async def login_providers_page(request: Request):
    """Admin page for configuring SSO login providers (Google, GitHub,
    Microsoft/Azure AD, generic OIDC/OAuth2) and local username/password
    accounts -- everything here lives in the database (auth_providers /
    local_users tables), not .env, so clients can self-serve their own
    identity provider without a redeploy. Restricted to admins, same
    pattern as /users above."""
    if request.state.auth_enabled and not getattr(request.state, "is_admin", False):
        return RedirectResponse(url=f"{ROOT_PATH}/")

    return app.state.templates.TemplateResponse(
        "login_providers.html",
        {"request": request}
    )


@app.get("/raw", response_class=HTMLResponse)
async def raw_data(request: Request, machine_id: str = None):
    """Raw data page."""
    # Get recent events, optionally filtered by machine
    launches = await app.state.db.get_recent_sessions(limit=50, machine_id=machine_id)

    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    return app.state.templates.TemplateResponse(
        "raw_data.html",
        {
            "request": request,
            "recent_launches": launches,
            "selected_machine": selected_machine,
            "machine_id": machine_id
        }
    )


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    """Session detail page with conversation history."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([col[0] for col in cursor.description], row)
        )

        # Get session info (with machine details)
        async with conn.execute(
            """SELECT s.*, m.machine_name, m.logged_in_user, m.os_type
               FROM sessions s
               LEFT JOIN machines m ON s.machine_id = m.machine_id
               WHERE s.session_id = ?""", (session_id,)
        ) as cursor:
            session = await cursor.fetchone()

        if not session:
            return HTMLResponse("Session not found", status_code=404)

        # Enforce the same machine-scoping non-admins get everywhere else --
        # this route takes session_id as a path param, so the query-string
        # machine_id rewrite in enforce_auth never applies here on
        # its own (CWE-639: any authenticated non-admin could otherwise view
        # any other user's full conversation transcript by session_id).
        allowed = getattr(request.state, "allowed_machine_ids", None)
        if allowed is not None and session.get("machine_id") not in allowed:
            return HTMLResponse("Session not found", status_code=404)

        # Get conversation messages
        async with conn.execute(
            """SELECT * FROM messages
               WHERE session_id = ?
               ORDER BY timestamp ASC""",
            (session_id,)
        ) as cursor:
            messages = await cursor.fetchall()

        # Get token usage
        async with conn.execute(
            """SELECT SUM(input_tokens) as input, SUM(output_tokens) as output,
                      SUM(total_tokens) as total, SUM(estimated_cost_usd) as cost
               FROM token_usage WHERE session_id = ?""",
            (session_id,)
        ) as cursor:
            tokens = await cursor.fetchone()

    tags_by_session = await app.state.db.get_tags_for_sessions([session_id])
    all_tags = await app.state.db.get_all_tags()

    return app.state.templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "messages": messages,
            "tokens": tokens or {},
            "tags": tags_by_session.get(session_id, []),
            "all_tags": all_tags,
        }
    )


@app.get("/api/tags")
async def api_get_tags():
    """All tags with usage counts."""
    return JSONResponse(await app.state.db.get_all_tags())


@app.post("/api/tags")
async def api_create_tag(data: dict):
    """Create a custom tag. Body: {name, color?}."""
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        tag_id = await app.state.db.create_tag(name, data.get("color"))
    except sqlite3.IntegrityError:
        return JSONResponse({"error": f"Tag '{name}' already exists"}, status_code=409)
    return JSONResponse({"status": "created", "id": tag_id})


async def _session_scope_guard(request: Request, session_id: str):
    """Shared guard for every /api/sessions/{session_id}/... route: 404s a
    session that doesn't exist, and 404s one outside the caller's allowed
    machine scope (path params bypass the query-string machine_id rewrite --
    see _check_machine_scope). Returns a JSONResponse to send back
    immediately if blocked, or None if the caller may proceed."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        async with conn.execute(
            "SELECT machine_id FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    allowed = getattr(request.state, "allowed_machine_ids", None)
    if allowed is not None and row["machine_id"] not in allowed:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return None


@app.get("/api/sessions/{session_id}/tags")
async def api_get_session_tags(request: Request, session_id: str):
    denied = await _session_scope_guard(request, session_id)
    if denied:
        return denied
    tags_by_session = await app.state.db.get_tags_for_sessions([session_id])
    return JSONResponse(tags_by_session.get(session_id, []))


@app.post("/api/sessions/{session_id}/tags")
async def api_add_session_tag(request: Request, session_id: str, data: dict):
    """Apply a tag to a session. Body: {tag: "bugfix"}."""
    denied = await _session_scope_guard(request, session_id)
    if denied:
        return denied
    tag_name = (data.get("tag") or "").strip()
    if not tag_name:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    email = (auth_utils.current_user(request) or {}).get("email") if request.state.auth_enabled else None
    tag = await app.state.db.add_session_tag(session_id, tag_name, source="manual", tagged_by=email)
    return JSONResponse({"status": "ok", "tag": tag})


@app.delete("/api/sessions/{session_id}/tags/{tag_id}")
async def api_remove_session_tag(request: Request, session_id: str, tag_id: int):
    denied = await _session_scope_guard(request, session_id)
    if denied:
        return denied
    removed = await app.state.db.remove_session_tag(session_id, tag_id)
    if not removed:
        return JSONResponse({"error": "Tag not applied to this session"}, status_code=404)
    return JSONResponse({"status": "removed"})


@app.get("/api/sessions/{session_id}/tags/suggest")
async def api_suggest_session_tags(request: Request, session_id: str):
    """Keyword-based tag suggestions the user can accept or ignore."""
    denied = await _session_scope_guard(request, session_id)
    if denied:
        return denied
    suggestions = await app.state.db.suggest_tags_for_session(session_id)
    return JSONResponse({"suggestions": suggestions})


# Client data ingestion endpoints (for remote clients to send data)
@app.post("/api/client/register")
async def register_client(request: Request, data: dict):
    """Register a client machine."""
    machine_id = data.get('machine_id')
    machine_name = data.get('machine_name')
    ip_address = data.get('ip_address')
    os_type = data.get('os_type')
    os_version = data.get('os_version')
    logged_in_user = data.get('logged_in_user')
    agent_version = data.get('agent_version')

    if not machine_id:
        return JSONResponse({"status": "error", "message": "machine_id required"}, status_code=400)

    # Check the token BEFORE creating/touching the row for an *existing*
    # machine_id (so someone else's registered machine can't be silently
    # re-registered/tampered with by an attacker who doesn't know its
    # token); brand-new machine_ids fall through allowed (nothing to spoof
    # yet) and get a token issued right after creation below.
    #
    # This same call also performs the one-time NULL -> token issuance for
    # a pre-existing/legacy machine_id (row exists, no agent_token yet).
    # That issuance is a single conditional UPDATE ... WHERE agent_token
    # IS NULL, so it can only ever succeed once per machine_id. A
    # previous version of this endpoint discarded this `issued` value and
    # re-derived it via a second verify_or_issue_agent_token() call below
    # — but by then the column was no longer NULL, so the second call's
    # own match check (`provided == stored_token`) failed (the caller's
    # `provided` is still the *old*/absent token), permanently losing the
    # freshly-issued token and locking the machine out of every future
    # request. Keep this value and only fall back to a second call when
    # it's actually needed (brand-new machine_id, whose row didn't exist
    # yet for this first call).
    provided = request.headers.get("x-agent-token")
    allowed, issued = await app.state.db.verify_or_issue_agent_token(
        machine_id, provided, allow_unregistered=True
    )
    if not allowed:
        return JSONResponse(
            {"detail": "Invalid or missing agent token for this machine_id"},
            status_code=403,
        )

    await app.state.db.register_machine(
        machine_id=machine_id,
        machine_name=machine_name,
        ip_address=ip_address,
        os_type=os_type,
        os_version=os_version,
        logged_in_user=logged_in_user,
        agent_version=agent_version
    )

    # Only re-check/issue if the gate call above couldn't hand back a
    # token yet — i.e. a brand-new machine_id, whose row didn't exist
    # until register_machine() just created it above.
    if issued is None:
        _, issued = await app.state.db.verify_or_issue_agent_token(machine_id, provided)

    resp = {"status": "registered", "machine_id": machine_id}
    if issued:
        resp["agent_token"] = issued
    return JSONResponse(resp)


@app.post("/api/client/heartbeat")
async def client_heartbeat(request: Request, data: dict):
    """Update machine heartbeat."""
    machine_id = data.get('machine_id')
    denied = await _require_agent_token(request, machine_id)
    if denied:
        return denied
    await app.state.db.update_machine_heartbeat(machine_id, agent_version=data.get('agent_version'))
    return JSONResponse({"status": "ok"})


@app.get("/api/agent/version")
async def api_agent_version():
    """Report the version of client_agent.py currently served under
    /static — the source of truth for the auto-update check. Reading it
    directly off the static file (instead of a separate hardcoded
    constant) guarantees this endpoint can never drift out of sync with
    what a freshly-downloaded agent would actually get. The actual trust
    boundary for auto-update is the Ed25519 signature at
    /static/client_agent.py.sig (see scripts/sign_agent.py and
    check_for_update() in client_agent.py) -- sha256 here is informational
    only, for operators comparing a deployed file by hand."""
    try:
        sha256 = hashlib.sha256((static_dir / "client_agent.py").read_bytes()).hexdigest()
    except Exception:
        sha256 = None
    return JSONResponse({"version": get_agent_version(), "sha256": sha256})


@app.post("/api/client/sync-log")
async def log_client_sync(request: Request, data: dict):
    """Record the result of one client-agent sync cycle (item counts, etc)."""
    machine_id = data.get('machine_id')
    denied = await _require_agent_token(request, machine_id)
    if denied:
        return denied

    await app.state.db.log_sync(
        machine_id=machine_id,
        source=data.get('source'),
        items_total=data.get('items_total', 0),
        items_synced=data.get('items_synced', 0),
        items_failed=data.get('items_failed', 0),
        message=data.get('message')
    )
    return JSONResponse({"status": "logged"})


@app.post("/api/client/session")
async def create_client_session(request: Request, data: dict):
    """Create a session from client machine."""
    denied = await _require_agent_token(request, data.get('machine_id'))
    if denied:
        return denied
    denied = await _deny_foreign_session(data.get('session_id'), data.get('machine_id'))
    if denied:
        return denied

    # Check if session already exists
    async with app.state.db.get_connection() as conn:
        async with conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ? AND machine_id = ? LIMIT 1",
            (data['session_id'], data['machine_id'])
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            return JSONResponse({"status": "already_exists"})

    await app.state.db.create_session(
        session_id=data['session_id'],
        start_time=data['start_time'],
        working_directory=data['working_directory'],
        command_args=data.get('command_args'),
        model_name=data.get('model_name'),
        machine_id=data.get('machine_id', 'local')
    )

    # Every new session corresponds to one Claude Code invocation, so log
    # it as a launch too. Without this, the `launches` table (and the
    # "total_launches" stat derived from it) never gets populated, since
    # client_agent.py only ever calls this endpoint and has no separate
    # launch-reporting path.
    await app.state.db.log_launch(
        session_id=data['session_id'],
        timestamp=data['start_time'],
        working_directory=data['working_directory'],
        command_line=data.get('command_args') or 'claude',
        machine_id=data.get('machine_id', 'local')
    )
    return JSONResponse({"status": "created"})


@app.post("/api/client/session/update")
async def update_client_session(request: Request, data: dict):
    """Update an existing session (end_time, duration, model, etc).

    Clients (esp. the Windows JSONL sync path) may occasionally report a
    corrupt/incomplete end_time (e.g. when a JSONL file lacks real start
    metadata and the agent falls back to "now" as start_time, while the
    actual end_time comes from stale file content earlier than that).
    Validate against the session's real start_time before trusting it, and
    always (re)compute duration_seconds server-side from the timestamps
    rather than trusting a client-computed value, so the two can never
    drift apart or go negative.
    """
    session_id = data.get('session_id')
    if not session_id:
        return JSONResponse({"status": "error", "message": "session_id required"}, status_code=400)

    # This payload doesn't carry machine_id, so look up which machine the
    # session actually belongs to and gate the token check on *that* --
    # otherwise anyone who can guess/enumerate a session_id could tamper
    # with another machine's session regardless of their own agent_token.
    async with app.state.db.get_connection() as conn:
        async with conn.execute(
            "SELECT machine_id FROM sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            owner_row = await cur.fetchone()
    if owner_row is None:
        return JSONResponse({"status": "error", "message": "session not found"}, status_code=404)
    denied = await _require_agent_token(request, owner_row[0])
    if denied:
        return denied

    updates = []
    params = []

    end_time = data.get('end_time')
    if end_time is not None:
        async with app.state.db.get_connection() as conn:
            async with conn.execute(
                "SELECT start_time FROM sessions WHERE session_id = ?", (session_id,)
            ) as cur:
                row = await cur.fetchone()
        start_time = row[0] if row else None

        valid = True
        if start_time:
            try:
                s_dt = datetime.fromisoformat(str(start_time).replace('Z', '+00:00'))
                e_dt = datetime.fromisoformat(str(end_time).replace('Z', '+00:00'))
                if e_dt < s_dt:
                    valid = False
                else:
                    data['duration_seconds'] = int((e_dt - s_dt).total_seconds())
            except (ValueError, TypeError):
                # Unparseable timestamp — don't trust it, but don't block
                # other fields (model_name/exit_code) from being applied.
                valid = False

        if valid:
            updates.append("end_time = ?")
            params.append(end_time)
        else:
            print(f"⚠ Rejected invalid end_time for session {session_id}: "
                  f"end_time={end_time!r} start_time={start_time!r}")
            data.pop('duration_seconds', None)

    for field in ('duration_seconds', 'model_name', 'exit_code'):
        if data.get(field) is not None:
            updates.append(f"{field} = ?")
            params.append(data[field])

    if not updates:
        return JSONResponse({"status": "no_changes"})

    params.append(session_id)
    async with app.state.db.get_connection() as conn:
        await conn.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
            params
        )
        await conn.commit()
    return JSONResponse({"status": "updated"})


@app.post("/api/client/token_usage")
async def log_client_token_usage(request: Request, data: dict):
    """Log token usage from client machine."""
    denied = await _require_agent_token(request, data.get('machine_id', 'local'))
    if denied:
        return denied
    denied = await _deny_foreign_session(data.get('session_id'), data.get('machine_id', 'local'))
    if denied:
        return denied
    # Check if already exists to prevent duplicates
    async with app.state.db.get_connection() as conn:
        async with conn.execute(
            """SELECT id FROM token_usage
               WHERE timestamp = ? AND session_id = ? AND machine_id = ?
               AND input_tokens = ? AND output_tokens = ?
               LIMIT 1""",
            (data['timestamp'], data['session_id'], data.get('machine_id', 'local'),
             data.get('input_tokens', 0), data.get('output_tokens', 0))
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            return JSONResponse({"status": "already_exists"})

    await app.state.db.log_token_usage(
        timestamp=data['timestamp'],
        session_id=data['session_id'],
        machine_id=data.get('machine_id', 'local'),
        model_name=data.get('model_name'),
        input_tokens=data.get('input_tokens'),
        output_tokens=data.get('output_tokens'),
        total_tokens=data.get('total_tokens'),
        estimated_cost_usd=data.get('estimated_cost_usd')
    )
    return JSONResponse({"status": "logged"})


@app.post("/api/client/message")
async def log_client_message(request: Request, data: dict):
    """Log a message from client machine."""
    denied = await _require_agent_token(request, data.get('machine_id'))
    if denied:
        return denied
    denied = await _deny_foreign_session(data.get('session_id'), data.get('machine_id'))
    if denied:
        return denied
    async with app.state.db.get_connection() as conn:
        # Dedup check
        async with conn.execute(
            """SELECT id FROM messages
               WHERE session_id = ? AND machine_id = ? AND timestamp = ? AND role = ?
               LIMIT 1""",
            (data['session_id'], data['machine_id'], data['timestamp'], data['role'])
        ) as cursor:
            if await cursor.fetchone():
                return JSONResponse({"status": "already_exists"})

        await conn.execute(
            """INSERT INTO messages
               (session_id, machine_id, timestamp, role, content, tokens, model)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (data['session_id'], data['machine_id'], data['timestamp'],
             data['role'], data['content'], data.get('tokens'), data.get('model'))
        )
        await conn.commit()
    return JSONResponse({"status": "logged"})


# Machine management API endpoints
@app.get("/api/machines")
async def api_get_machines(request: Request):
    """Get all registered machines (or just the caller's own machine(s) if
    they aren't an admin -- see enforce_auth/request.state.allowed_machine_ids)."""
    machines = await app.state.db.get_all_machines()
    allowed = getattr(request.state, "allowed_machine_ids", None)
    if allowed is not None:
        machines = [m for m in machines if m["machine_id"] in allowed]
    return JSONResponse(machines)


@app.get("/api/machines/active")
async def api_get_active_machines(request: Request):
    """Get active machines (scoped the same way as /api/machines)."""
    machines = await app.state.db.get_active_machines()
    allowed = getattr(request.state, "allowed_machine_ids", None)
    if allowed is not None:
        machines = [m for m in machines if m["machine_id"] in allowed]
    return JSONResponse(machines)


# Admin user management API endpoints (admin-only; see admin_users table and
# Database.is_admin_user/get_machine_ids_for_user).
@app.get("/api/admin/users")
async def api_get_admin_users(request: Request):
    """List admin users -- the only users who can see every machine's data
    instead of just their own."""
    denied = _require_admin(request)
    if denied:
        return denied
    admins = await app.state.db.get_admin_users()
    return JSONResponse(admins)


@app.post("/api/admin/users")
async def api_add_admin_user(request: Request, data: dict):
    """Grant admin access to an email. Body: {email}."""
    denied = _require_admin(request)
    if denied:
        return denied
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)
    await app.state.db.add_admin_user(email)
    return JSONResponse({"status": "added"})


@app.delete("/api/admin/users/{email}")
async def api_remove_admin_user(request: Request, email: str):
    """Revoke admin access from an email. Refuses to remove the last
    remaining admin so the team can't accidentally lock everyone out."""
    denied = _require_admin(request)
    if denied:
        return denied
    email = email.strip().lower()
    admins = await app.state.db.get_admin_users()
    if len(admins) <= 1 and any(a["email"] == email for a in admins):
        return JSONResponse({"error": "Cannot remove the last remaining admin"}, status_code=400)
    removed = await app.state.db.remove_admin_user(email)
    if not removed:
        return JSONResponse({"error": "Admin not found"}, status_code=404)
    return JSONResponse({"status": "removed"})


# Super user management API endpoints (admin-only; gates access to the
# Conversation Tone feature specifically - see super_users table/DB API).
@app.get("/api/admin/super-users")
async def api_get_super_users(request: Request):
    """List super users -- the only users who can see Conversation Tone."""
    denied = _require_admin(request)
    if denied:
        return denied
    super_users = await app.state.db.get_super_users()
    return JSONResponse(super_users)


@app.post("/api/admin/machines/{machine_id}/reset-token")
async def api_reset_machine_token(request: Request, machine_id: str):
    """Clear a stuck machine's stored agent_token so it can register
    again as if it were a legacy (pre-token) machine and receive a fresh
    one. Use when a machine's local .agent_token file was lost/reset, or
    (as happened for every agent that auto-updated on 2026-10-01) was
    never actually delivered due to a server-side bug."""
    denied = _require_admin(request)
    if denied:
        return denied
    reset = await app.state.db.reset_agent_token(machine_id)
    if not reset:
        return JSONResponse({"error": "machine_id not found"}, status_code=404)
    return JSONResponse({"status": "reset", "machine_id": machine_id})


@app.post("/api/admin/super-users")
async def api_add_super_user(request: Request, data: dict):
    """Grant super-user access to an email. Body: {email}."""
    denied = _require_admin(request)
    if denied:
        return denied
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)
    await app.state.db.add_super_user(email)
    return JSONResponse({"status": "added"})


@app.delete("/api/admin/super-users/{email}")
async def api_remove_super_user(request: Request, email: str):
    """Revoke super-user access from an email."""
    denied = _require_admin(request)
    if denied:
        return denied
    email = email.strip().lower()
    removed = await app.state.db.remove_super_user(email)
    if not removed:
        return JSONResponse({"error": "Super user not found"}, status_code=404)
    return JSONResponse({"status": "removed"})


# Login provider (SSO) + local account admin API endpoints -- see
# "LOGIN PROVIDERS" section of database.py and src/utils/auth.py. Every
# provider's client secret is encrypted at rest (src/utils/crypto.py) and
# never sent back to the browser once saved (see
# Database._auth_provider_row_to_dict's has_secret flag).
@app.get("/api/admin/auth-providers")
async def api_list_auth_providers(request: Request):
    """List every configured login provider (client secrets masked), plus
    the built-in presets so the admin UI can auto-fill known endpoints."""
    denied = _require_admin(request)
    if denied:
        return denied
    providers = await app.state.db.list_auth_providers()
    return JSONResponse({"providers": providers, "presets": auth_utils.PROVIDER_PRESETS})


@app.post("/api/admin/auth-providers")
async def api_create_auth_provider(request: Request, data: dict):
    """Add a new login provider. Body: {provider_key, provider_type,
    display_name, client_id, client_secret, authorization_url?, token_url?,
    userinfo_url?, scopes?, tenant_id?, enabled?}. provider_key must be a
    unique URL-safe slug (e.g. "google", "github", "azure-ad", or a custom
    one for multiple generic_oauth2 providers)."""
    denied = _require_admin(request)
    if denied:
        return denied
    provider_key = (data.get("provider_key") or "").strip().lower()
    provider_type = (data.get("provider_type") or "").strip()
    client_id = (data.get("client_id") or "").strip()
    client_secret = data.get("client_secret") or ""
    if not provider_key or provider_type not in ("azure_ad", "google", "github", "generic_oauth2"):
        return JSONResponse({"error": "provider_key and a valid provider_type are required"}, status_code=400)
    if not client_id or not client_secret:
        return JSONResponse({"error": "client_id and client_secret are required"}, status_code=400)
    try:
        provider_id = await app.state.db.create_auth_provider(
            provider_key=provider_key,
            provider_type=provider_type,
            display_name=(data.get("display_name") or provider_key).strip(),
            client_id=client_id,
            client_secret_encrypted=encrypt_secret(client_secret),
            authorization_url=data.get("authorization_url"),
            token_url=data.get("token_url"),
            userinfo_url=data.get("userinfo_url"),
            scopes=data.get("scopes"),
            tenant_id=data.get("tenant_id"),
            enabled=bool(data.get("enabled", True)),
        )
    except sqlite3.IntegrityError:
        return JSONResponse({"error": f"provider_key '{provider_key}' is already in use"}, status_code=409)
    return JSONResponse({"status": "created", "id": provider_id})


@app.put("/api/admin/auth-providers/{provider_id}")
async def api_update_auth_provider(request: Request, provider_id: int, data: dict):
    """Update a provider's config. Only client_secret is re-encrypted; omit
    it (or send an empty string) to keep the existing one unchanged."""
    denied = _require_admin(request)
    if denied:
        return denied
    fields = {
        k: v for k, v in data.items()
        if k in (
            "display_name", "client_id", "authorization_url", "token_url",
            "userinfo_url", "scopes", "tenant_id", "enabled",
        )
    }
    if data.get("client_secret"):
        fields["client_secret_encrypted"] = encrypt_secret(data["client_secret"])
    updated = await app.state.db.update_auth_provider(provider_id, **fields)
    if not updated:
        return JSONResponse({"error": "Provider not found or no changes given"}, status_code=404)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/auth-providers/{provider_id}")
async def api_delete_auth_provider(request: Request, provider_id: int):
    """Remove a login provider entirely."""
    denied = _require_admin(request)
    if denied:
        return denied
    removed = await app.state.db.delete_auth_provider(provider_id)
    if not removed:
        return JSONResponse({"error": "Provider not found"}, status_code=404)
    return JSONResponse({"status": "removed"})


@app.get("/api/admin/local-users")
async def api_list_local_users(request: Request):
    """List admin-created username/password accounts (password hashes
    excluded)."""
    denied = _require_admin(request)
    if denied:
        return denied
    return JSONResponse(await app.state.db.list_local_users())


@app.post("/api/admin/local-users")
async def api_create_local_user(request: Request, data: dict):
    """Create a local account. Body: {email, name, password}. There's no
    self-service signup (per this project's design) -- only admins can
    create local accounts, from this page."""
    denied = _require_admin(request)
    if denied:
        return denied
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return JSONResponse({"error": "email and password are required"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "password must be at least 8 characters"}, status_code=400)
    try:
        user_id = await app.state.db.create_local_user(
            email=email,
            name=(data.get("name") or "").strip(),
            password_hash=hash_password(password),
        )
    except sqlite3.IntegrityError:
        return JSONResponse({"error": f"'{email}' is already registered"}, status_code=409)
    return JSONResponse({"status": "created", "id": user_id})


@app.put("/api/admin/local-users/{user_id}")
async def api_update_local_user(request: Request, user_id: int, data: dict):
    """Update a local account's name/enabled flag, or reset its password.
    Body may include any of: {name, enabled, password}."""
    denied = _require_admin(request)
    if denied:
        return denied
    fields = {k: v for k, v in data.items() if k in ("name", "enabled")}
    if data.get("password"):
        if len(data["password"]) < 8:
            return JSONResponse({"error": "password must be at least 8 characters"}, status_code=400)
        fields["password_hash"] = hash_password(data["password"])
    updated = await app.state.db.update_local_user(user_id, **fields)
    if not updated:
        return JSONResponse({"error": "Account not found or no changes given"}, status_code=404)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/local-users/{user_id}")
async def api_delete_local_user(request: Request, user_id: int):
    """Remove a local account entirely."""
    denied = _require_admin(request)
    if denied:
        return denied
    removed = await app.state.db.delete_local_user(user_id)
    if not removed:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    return JSONResponse({"status": "removed"})


# Group (team) management API endpoints
@app.get("/api/groups")
async def api_get_groups():
    """Get all groups (teams), each with its member count."""
    groups = await app.state.db.get_all_groups()
    return JSONResponse(groups)


@app.post("/api/groups")
async def api_create_group(request: Request, data: dict):
    """Create a new group (team). Body: {name, description?}."""
    denied = _require_admin(request)
    if denied:
        return denied
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        group_id = await app.state.db.create_group(name, data.get("description"))
    except sqlite3.IntegrityError:
        return JSONResponse({"error": f"A group named '{name}' already exists"}, status_code=409)
    return JSONResponse({"status": "created", "id": group_id})


@app.put("/api/groups/{group_id}")
async def api_update_group(request: Request, group_id: int, data: dict):
    """Rename/update a group. Body: {name?, description?}."""
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        updated = await app.state.db.update_group(group_id, data.get("name"), data.get("description"))
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "A group with that name already exists"}, status_code=409)
    if not updated:
        return JSONResponse({"error": "Group not found"}, status_code=404)
    return JSONResponse({"status": "updated"})


@app.delete("/api/groups/{group_id}")
async def api_delete_group(request: Request, group_id: int):
    """Delete a group. Member machines are unassigned, not deleted."""
    denied = _require_admin(request)
    if denied:
        return denied
    deleted = await app.state.db.delete_group(group_id)
    if not deleted:
        return JSONResponse({"error": "Group not found"}, status_code=404)
    return JSONResponse({"status": "deleted"})


@app.get("/api/groups/{group_id}/machines")
async def api_get_group_machines(group_id: int, request: Request):
    """Get all machines that belong to a group (scoped the same way as
    /api/machines for non-admins)."""
    machines = await app.state.db.get_machines_in_group(group_id)
    allowed = getattr(request.state, "allowed_machine_ids", None)
    if allowed is not None:
        machines = [m for m in machines if m["machine_id"] in allowed]
    return JSONResponse(machines)


@app.put("/api/machines/{machine_id}/group")
async def api_set_machine_group(request: Request, machine_id: str, data: dict):
    """Assign (or clear, if group_id is null) a machine's group. Body:
    {group_id: int|null}. Admin-only: team assignment now lives in the
    User Management screen, which only admins can reach."""
    denied = _require_admin(request)
    if denied:
        return denied
    group_id = data.get("group_id")
    try:
        updated = await app.state.db.set_machine_group(machine_id, group_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not updated:
        return JSONResponse({"error": "Machine not found"}, status_code=404)
    return JSONResponse({"status": "updated"})


@app.delete("/api/machines/{machine_id}")
async def api_delete_machine(request: Request, machine_id: str):
    """Permanently delete a user/machine and every row referencing it
    (sessions, tokens, messages, code analytics, skills/achievements, etc).
    Admin-only and irreversible -- see Database.delete_machine_completely."""
    denied = _require_admin(request)
    if denied:
        return denied
    result = await app.state.db.delete_machine_completely(machine_id)
    if result is None:
        return JSONResponse({"error": "Machine not found"}, status_code=404)
    return JSONResponse({"status": "deleted", **result})


@app.get("/api/sync-log")
async def api_get_sync_log(machine_id: str = None, limit: int = 200):
    """Get recent client-agent sync cycle results (item counts, etc)."""
    sync_log = await app.state.db.get_sync_log(limit=limit, machine_id=machine_id)
    return JSONResponse(sync_log)


# API endpoints
@app.get("/api/stats/overview")
async def api_stats_overview(machine_id: str = None):
    """Get overview statistics."""
    stats = await app.state.db.get_stats_overview(machine_id=machine_id)
    return JSONResponse(stats)


@app.get("/api/stats/sessions")
async def api_stats_sessions(days: int = 30, machine_id: str = None):
    """Get session statistics by date."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))
        query = "SELECT DATE(start_time) as date, COUNT(*) as count FROM sessions WHERE start_time >= datetime('now', '-' || ? || ' days')"
        params = [days]
        frag, fparams = Database._machine_scope_clause(machine_id)
        query += frag
        params.extend(fparams)
        query += " GROUP BY DATE(start_time) ORDER BY date"
        async with conn.execute(query, params) as cursor:
            data = [dict(r) for r in await cursor.fetchall()]
    return JSONResponse(data)


@app.get("/api/stats/tokens")
async def api_stats_tokens(days: int = 30, machine_id: str = None):
    """Get token usage by date."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))
        query = """SELECT DATE(tu.timestamp) as date,
                          SUM(tu.input_tokens) as input_tokens,
                          SUM(tu.output_tokens) as output_tokens,
                          SUM(tu.total_tokens) as total_tokens
                   FROM token_usage tu"""
        params = [days]
        frag, fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        if frag:
            query += f" JOIN sessions s ON tu.session_id = s.session_id WHERE tu.timestamp >= datetime('now', '-' || ? || ' days'){frag}"
            params.extend(fparams)
        else:
            query += " WHERE tu.timestamp >= datetime('now', '-' || ? || ' days')"
        query += " GROUP BY DATE(tu.timestamp) ORDER BY date"
        async with conn.execute(query, params) as cursor:
            data = [dict(r) for r in await cursor.fetchall()]
    return JSONResponse(data)


@app.get("/api/stats/repositories")
async def api_stats_repositories(limit: int = 10, machine_id: str = None):
    """Get top projects (working directories) by activity."""
    data = await app.state.db.get_top_projects(limit=limit, machine_id=machine_id)
    return JSONResponse(data)


@app.get("/api/stats/daily-usage")
async def api_stats_daily_usage(days: int = 30, machine_id: str = None):
    """Get daily Claude usage in minutes, grouped by machine."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([col[0] for col in cursor.description], row)
        )

        query = f"""
            SELECT
                date(start_time) as date,
                m.machine_name,
                m.logged_in_user,
                s.machine_id,
                COUNT(*) as session_count,
                COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) as total_seconds,
                ROUND(COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) / 60.0, 1) as total_minutes
            FROM sessions s
            LEFT JOIN machines m ON s.machine_id = m.machine_id
            WHERE s.start_time >= date('now', ? || ' days')
        """
        params = [f"-{days}"]

        frag, fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        query += frag
        params.extend(fparams)

        query += """
            GROUP BY date(start_time), s.machine_id
            ORDER BY date(start_time) DESC
        """

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        # Also compute grand totals
        totals_query = f"""
            SELECT
                s.machine_id,
                m.machine_name,
                m.logged_in_user,
                COUNT(*) as session_count,
                COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) as total_seconds,
                ROUND(COALESCE(SUM(MIN(s.duration_seconds, {MAX_SESSION_DURATION_SECONDS})), 0) / 60.0, 1) as total_minutes
            FROM sessions s
            LEFT JOIN machines m ON s.machine_id = m.machine_id
            WHERE s.start_time >= date('now', ? || ' days')
        """
        totals_params = [f"-{days}"]
        totals_frag, totals_fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        totals_query += totals_frag
        totals_params.extend(totals_fparams)
        totals_query += " GROUP BY s.machine_id"

        async with conn.execute(totals_query, totals_params) as cursor:
            totals = await cursor.fetchall()

    return JSONResponse({"daily": rows, "totals": totals})


# ── Insights API endpoints ──────────────────────────────────────────

@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, machine_id: str = None):
    """Analytics insights page."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)
    return app.state.templates.TemplateResponse(
        "insights.html",
        {"request": request, "selected_machine": selected_machine, "machine_id": machine_id}
    )


@app.get("/api/insights/cost-per-user")
async def api_insights_cost_per_user(days: int = 30, machine_id: str = None):
    """Cost breakdown per user/machine."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT m.machine_name, m.logged_in_user, s.machine_id,
                      COUNT(DISTINCT s.session_id) as sessions,
                      COALESCE(SUM(tu.input_tokens),0) as input_tokens,
                      COALESCE(SUM(tu.output_tokens),0) as output_tokens,
                      COALESCE(SUM(tu.estimated_cost_usd),0) as cost,
                      COALESCE(SUM(s.duration_seconds),0) as total_seconds
               FROM sessions s
               LEFT JOIN machines m ON s.machine_id = m.machine_id
               LEFT JOIN token_usage tu ON s.session_id = tu.session_id
               WHERE s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY s.machine_id ORDER BY cost DESC"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()

        # Daily trend per user
        q2 = """SELECT date(s.start_time) as date, s.machine_id,
                       m.logged_in_user,
                       COALESCE(SUM(tu.estimated_cost_usd),0) as cost
                FROM sessions s
                LEFT JOIN machines m ON s.machine_id = m.machine_id
                LEFT JOIN token_usage tu ON s.session_id = tu.session_id
                WHERE s.start_time >= date('now', ? || ' days')"""
        p2 = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q2 += _frag
        p2.extend(_fparams)
        q2 += " GROUP BY date(s.start_time), s.machine_id ORDER BY date"
        async with conn.execute(q2, p2) as cur:
            daily = await cur.fetchall()

    return JSONResponse({"summary": data, "daily": daily})


@app.get("/api/insights/tool-usage")
async def api_insights_tool_usage(days: int = 30, machine_id: str = None):
    """Tool usage frequency from messages."""
    import re as _re
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT msg.content FROM messages msg
               JOIN sessions s ON msg.session_id = s.session_id
               WHERE msg.role = 'assistant'
               AND s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        async with conn.execute(q, p) as cur:
            rows = await cur.fetchall()

    # Parse tool calls from message content (format: 🔧 ToolName(...))
    tool_counts = {}
    for row in rows:
        content = row.get('content', '')
        for match in _re.findall(r'🔧\s*(\w+)\(', content):
            tool_counts[match] = tool_counts.get(match, 0) + 1

    tools = sorted(tool_counts.items(), key=lambda x: -x[1])
    return JSONResponse({"tools": [{"name": t, "count": c} for t, c in tools]})


@app.get("/api/insights/model-usage")
async def api_insights_model_usage(days: int = 30, machine_id: str = None):
    """Model usage breakdown."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT tu.model_name,
                      COUNT(*) as call_count,
                      SUM(tu.input_tokens) as input_tokens,
                      SUM(tu.output_tokens) as output_tokens,
                      SUM(tu.total_tokens) as total_tokens,
                      ROUND(SUM(tu.estimated_cost_usd), 4) as cost
               FROM token_usage tu
               JOIN sessions s ON tu.session_id = s.session_id
               WHERE s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY tu.model_name ORDER BY cost DESC"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()
    return JSONResponse(data)


@app.get("/api/insights/peak-hours")
async def api_insights_peak_hours(days: int = 90, machine_id: str = None):
    """Peak usage hours heatmap (day-of-week × hour)."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT
                   CAST(strftime('%w', start_time) AS INTEGER) as dow,
                   CAST(strftime('%H', start_time) AS INTEGER) as hour,
                   COUNT(*) as count
               FROM sessions
               WHERE start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY dow, hour"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()
    return JSONResponse(data)


@app.get("/api/insights/session-complexity")
async def api_insights_session_complexity(days: int = 30, machine_id: str = None):
    """Session complexity: turns, duration, cost distribution."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT s.session_id,
                      s.duration_seconds,
                      s.working_directory,
                      COUNT(msg.id) as message_count,
                      COALESCE(SUM(CASE WHEN msg.role='user' THEN 1 ELSE 0 END),0) as user_turns,
                      COALESCE(SUM(CASE WHEN msg.role='assistant' THEN 1 ELSE 0 END),0) as assistant_turns,
                      (SELECT COALESCE(SUM(estimated_cost_usd),0) FROM token_usage WHERE session_id=s.session_id) as cost
               FROM sessions s
               LEFT JOIN messages msg ON s.session_id = msg.session_id
               WHERE s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY s.session_id ORDER BY message_count DESC"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()

    # Build distributions
    turn_buckets = {"1-2": 0, "3-5": 0, "6-10": 0, "11-20": 0, "21-50": 0, "50+": 0}
    dur_buckets = {"<5m": 0, "5-15m": 0, "15-30m": 0, "30-60m": 0, "1-2h": 0, "2h+": 0}
    for s in data:
        turns = s.get('user_turns', 0)
        if turns <= 2: turn_buckets["1-2"] += 1
        elif turns <= 5: turn_buckets["3-5"] += 1
        elif turns <= 10: turn_buckets["6-10"] += 1
        elif turns <= 20: turn_buckets["11-20"] += 1
        elif turns <= 50: turn_buckets["21-50"] += 1
        else: turn_buckets["50+"] += 1

        dur = s.get('duration_seconds') or 0
        if dur < 300: dur_buckets["<5m"] += 1
        elif dur < 900: dur_buckets["5-15m"] += 1
        elif dur < 1800: dur_buckets["15-30m"] += 1
        elif dur < 3600: dur_buckets["30-60m"] += 1
        elif dur < 7200: dur_buckets["1-2h"] += 1
        else: dur_buckets["2h+"] += 1

    return JSONResponse({
        "sessions": data[:50],
        "turn_distribution": turn_buckets,
        "duration_distribution": dur_buckets,
        "total": len(data),
        "avg_turns": round(sum(s['user_turns'] for s in data) / max(len(data), 1), 1),
        "avg_duration_min": round(sum((s.get('duration_seconds') or 0) for s in data) / max(len(data), 1) / 60, 1),
    })


@app.get("/api/insights/productivity")
async def api_insights_productivity(days: int = 30, machine_id: str = None):
    """Productivity: sessions vs git output."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        # Daily sessions + git activity
        frag, fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q = f"""SELECT date(s.start_time) as date,
                      COUNT(DISTINCT s.session_id) as sessions,
                      COALESCE(SUM(s.duration_seconds),0) as total_seconds,
                      COUNT(DISTINCT s.working_directory) as projects
               FROM sessions s
               WHERE s.start_time >= date('now', ? || ' days'){frag}
               GROUP BY date(s.start_time) ORDER BY date"""
        p = [f"-{days}"] + fparams
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()

        # Overall score
        total_sessions = sum(d['sessions'] for d in data)
        total_hours = round(sum(d['total_seconds'] for d in data) / 3600.0, 1)
        total_projects = sum(d['projects'] for d in data)

    return JSONResponse({
        "daily": data,
        "totals": {
            "sessions": total_sessions,
            "hours": total_hours,
            "projects": total_projects,
            "hours_per_session": round(total_hours / max(total_sessions, 1), 2),
            "projects_per_session": round(total_projects / max(total_sessions, 1), 2),
        }
    })


@app.get("/api/insights/project-heatmap")
async def api_insights_project_heatmap(days: int = 90, machine_id: str = None):
    """Project activity calendar heatmap."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT date(start_time) as date,
                      working_directory as project,
                      COUNT(*) as sessions,
                      COALESCE(SUM(duration_seconds),0) as total_seconds
               FROM sessions
               WHERE start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY date, project ORDER BY date"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()

    # Calendar data: date → total sessions
    calendar = {}
    project_totals = {}
    for row in data:
        d = row['date']
        calendar[d] = calendar.get(d, 0) + row['sessions']
        proj = (row['project'] or 'unknown').replace('\\', '/').split('/')[-1]
        project_totals[proj] = project_totals.get(proj, 0) + row['sessions']

    top_projects = sorted(project_totals.items(), key=lambda x: -x[1])[:15]
    return JSONResponse({
        "calendar": [{"date": d, "count": c} for d, c in sorted(calendar.items())],
        "top_projects": [{"name": n, "sessions": s} for n, s in top_projects]
    })


@app.get("/api/insights/cost-forecast")
async def api_insights_cost_forecast(machine_id: str = None):
    """Cost trend and simple forecast."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT date(tu.timestamp) as date,
                      ROUND(SUM(tu.estimated_cost_usd), 4) as cost
               FROM token_usage tu
               JOIN sessions s ON tu.session_id = s.session_id
               WHERE tu.timestamp >= date('now', '-60 days')"""
        p = []
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " GROUP BY date(tu.timestamp) ORDER BY date"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()

    # Simple linear forecast for next 30 days
    if len(data) >= 7:
        recent = data[-14:] if len(data) >= 14 else data
        avg_daily = sum(d['cost'] for d in recent) / len(recent)
    else:
        avg_daily = sum(d['cost'] for d in data) / max(len(data), 1) if data else 0

    forecast = []
    if data:
        last_date = data[-1]['date']
        for i in range(1, 31):
            d = (datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=i)).strftime('%Y-%m-%d')
            forecast.append({"date": d, "cost": round(avg_daily, 2)})

    return JSONResponse({
        "actual": data,
        "forecast": forecast,
        "avg_daily_cost": round(avg_daily, 2),
        "projected_monthly": round(avg_daily * 30, 2),
    })


@app.get("/api/insights/top-prompts")
async def api_insights_top_prompts(days: int = 30, machine_id: str = None):
    """Top user prompts (first message per session)."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT msg.content, msg.session_id, s.working_directory,
                      LENGTH(msg.content) as length
               FROM messages msg
               JOIN sessions s ON msg.session_id = s.session_id
               WHERE msg.role = 'user'
               AND s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " ORDER BY msg.timestamp ASC"
        async with conn.execute(q, p) as cur:
            rows = await cur.fetchall()

    # Get first prompt per session
    seen = set()
    prompts = []
    for r in rows:
        sid = r['session_id']
        if sid not in seen:
            seen.add(sid)
            text = (r['content'] or '')[:200].strip()
            if text and len(text) > 5:
                proj = (r['working_directory'] or '').replace('\\', '/').split('/')[-1]
                prompts.append({"text": text, "project": proj, "length": r['length']})

    # Word frequency
    import re as _re
    word_freq = {}
    stop = {'the','a','an','is','to','and','of','in','for','on','it','this','that','with','i','you','can','my','me','be','do','not','are','was','have','has','from','or','but','at','by','we','all','if','so'}
    for p in prompts:
        words = _re.findall(r'[a-zA-Z]{3,}', p['text'].lower())
        for w in words:
            if w not in stop:
                word_freq[w] = word_freq.get(w, 0) + 1
    top_words = sorted(word_freq.items(), key=lambda x: -x[1])[:40]

    return JSONResponse({
        "prompts": prompts[:30],
        "total": len(prompts),
        "avg_length": round(sum(p['length'] for p in prompts) / max(len(prompts), 1)),
        "top_words": [{"word": w, "count": c} for w, c in top_words],
    })


@app.get("/api/insights/session-timeline")
async def api_insights_session_timeline(days: int = 7, machine_id: str = None):
    """Session timeline for Gantt-style view."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        q = """SELECT s.session_id, s.start_time, s.end_time, s.duration_seconds,
                      s.working_directory, s.model_name, s.machine_id,
                      m.logged_in_user
               FROM sessions s
               LEFT JOIN machines m ON s.machine_id = m.machine_id
               WHERE s.start_time >= date('now', ? || ' days')"""
        p = [f"-{days}"]
        _frag, _fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += _frag
        p.extend(_fparams)
        q += " ORDER BY s.start_time"
        async with conn.execute(q, p) as cur:
            data = await cur.fetchall()
    return JSONResponse(data)


# ── HIGH-VALUE REPORTS ───────────────────────────────────────────────

@app.get("/api/insights/executive-kpi")
async def api_insights_executive_kpi(days: int = 30, machine_id: str = None):
    """Executive KPI dashboard: single-glance metrics."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

        def mf(alias="s"):
            return Database._machine_scope_clause(machine_id, f"{alias}.machine_id")

        extra, mp = mf("s")

        # Total cost, sessions, hours in period
        q = f"""SELECT COUNT(*) as total_sessions,
                       COALESCE(SUM(duration_seconds), 0) as total_seconds,
                       COUNT(DISTINCT working_directory) as unique_projects,
                       COUNT(DISTINCT machine_id) as active_machines
                FROM sessions s
                WHERE s.start_time >= date('now', ? || ' days'){extra}"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            sess = (await cur.fetchone()) or {}

        extra_tu, mp_tu = mf("tu")
        q2 = f"""SELECT COALESCE(SUM(estimated_cost_usd), 0) as total_cost,
                        COALESCE(SUM(input_tokens), 0) as total_input,
                        COALESCE(SUM(output_tokens), 0) as total_output
                 FROM token_usage tu
                 JOIN sessions s ON tu.session_id = s.session_id
                 WHERE tu.timestamp >= date('now', ? || ' days'){extra_tu}"""
        async with conn.execute(q2, [f"-{days}"] + mp_tu) as cur:
            tok = (await cur.fetchone()) or {}

        # Unique developers
        extra_m, mp_m = Database._machine_scope_clause(machine_id, "s.machine_id")
        q3 = f"""SELECT COUNT(DISTINCT m.logged_in_user) as unique_devs
                 FROM sessions s
                 JOIN machines m ON s.machine_id = m.machine_id
                 WHERE s.start_time >= date('now', ? || ' days')
                 AND m.logged_in_user IS NOT NULL{extra_m}"""
        async with conn.execute(q3, [f"-{days}"] + mp_m) as cur:
            devs_row = (await cur.fetchone()) or {}

        # Previous period for comparison
        q4 = f"""SELECT COALESCE(SUM(tu.estimated_cost_usd), 0) as prev_cost,
                        COUNT(DISTINCT s.session_id) as prev_sessions
                 FROM token_usage tu
                 JOIN sessions s ON tu.session_id = s.session_id
                 WHERE tu.timestamp >= date('now', ? || ' days')
                 AND tu.timestamp < date('now', ? || ' days'){extra_tu}"""
        async with conn.execute(q4, [f"-{days*2}", f"-{days}"] + mp_tu) as cur:
            prev = (await cur.fetchone()) or {}

        # Weekly trend (last 4 weeks)
        q5 = f"""SELECT strftime('%Y-W%W', tu.timestamp) as week,
                        ROUND(SUM(tu.estimated_cost_usd), 4) as cost,
                        COUNT(DISTINCT s.session_id) as sessions
                 FROM token_usage tu
                 JOIN sessions s ON tu.session_id = s.session_id
                 WHERE tu.timestamp >= date('now', '-28 days'){extra_tu}
                 GROUP BY week ORDER BY week"""
        async with conn.execute(q5, mp_tu) as cur:
            weekly = await cur.fetchall()

        total_cost = tok.get('total_cost', 0) or 0
        total_sessions = sess.get('total_sessions', 0) or 0
        total_seconds = sess.get('total_seconds', 0) or 0
        unique_devs = devs_row.get('unique_devs', 0) or 0
        total_input = tok.get('total_input', 0) or 0
        total_output = tok.get('total_output', 0) or 0
        prev_cost = prev.get('prev_cost', 0) or 0
        prev_sessions = prev.get('prev_sessions', 0) or 0

        cost_per_dev_day = round(total_cost / max(unique_devs * days, 1), 2)
        cost_per_session = round(total_cost / max(total_sessions, 1), 2)
        avg_session_min = round((total_seconds / max(total_sessions, 1)) / 60, 1)
        sessions_per_day = round(total_sessions / max(days, 1), 1)
        output_ratio = round(total_output / max(total_input, 1) * 100, 1)

        cost_change = round(((total_cost - prev_cost) / max(prev_cost, 0.001)) * 100, 1)
        session_change = round(((total_sessions - prev_sessions) / max(prev_sessions, 1)) * 100, 1)

    return JSONResponse({
        "kpis": {
            "total_cost": round(total_cost, 2),
            "total_sessions": total_sessions,
            "total_hours": round(total_seconds / 3600, 1),
            "unique_devs": unique_devs,
            "unique_projects": sess.get('unique_projects', 0) or 0,
            "active_machines": sess.get('active_machines', 0) or 0,
            "cost_per_dev_day": cost_per_dev_day,
            "cost_per_session": cost_per_session,
            "avg_session_min": avg_session_min,
            "sessions_per_day": sessions_per_day,
            "output_efficiency": output_ratio,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        },
        "trends": {
            "cost_change_pct": cost_change,
            "session_change_pct": session_change,
        },
        "weekly": weekly,
    })


@app.get("/api/insights/model-roi")
async def api_insights_model_roi(days: int = 30, machine_id: str = None):
    """Model ROI comparison: cost efficiency per model."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "tu.machine_id")

        q = f"""SELECT tu.model_name,
                       COUNT(*) as api_calls,
                       COUNT(DISTINCT tu.session_id) as sessions,
                       SUM(tu.input_tokens) as total_input,
                       SUM(tu.output_tokens) as total_output,
                       ROUND(SUM(tu.estimated_cost_usd), 4) as total_cost,
                       ROUND(AVG(tu.output_tokens), 0) as avg_output_per_call,
                       ROUND(AVG(tu.input_tokens), 0) as avg_input_per_call
                FROM token_usage tu
                WHERE tu.timestamp >= date('now', ? || ' days'){extra}
                AND tu.output_tokens > 0
                GROUP BY tu.model_name
                ORDER BY total_cost DESC"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            models = await cur.fetchall()

        # Enrich with calculated metrics
        result = []
        for m in models:
            total_out = m['total_output'] or 1
            total_in = m['total_input'] or 1
            cost = m['total_cost'] or 0
            result.append({
                **m,
                "cost_per_1k_output": round(cost / (total_out / 1000), 2) if total_out > 0 else 0,
                "cost_per_1k_input": round(cost / (total_in / 1000), 2) if total_in > 0 else 0,
                "output_input_ratio": round(total_out / total_in * 100, 1),
                "cost_per_session": round(cost / max(m['sessions'], 1), 2),
                "cost_per_call": round(cost / max(m['api_calls'], 1), 6),
            })

        # Best model by different criteria
        cheapest = min(result, key=lambda x: x['cost_per_1k_output']) if result else None
        most_output = max(result, key=lambda x: x['avg_output_per_call']) if result else None
        most_used = max(result, key=lambda x: x['api_calls']) if result else None

    return JSONResponse({
        "models": result,
        "recommendations": {
            "cheapest_per_output": cheapest['model_name'] if cheapest else None,
            "most_productive": most_output['model_name'] if most_output else None,
            "most_popular": most_used['model_name'] if most_used else None,
        }
    })


@app.get("/api/insights/budget-tracker")
async def api_insights_budget_tracker(monthly_budget: float = 500.0, machine_id: str = None):
    """Token budget tracker with burn rate and projections."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "tu.machine_id")

        # Current month spend
        q = f"""SELECT ROUND(SUM(tu.estimated_cost_usd), 4) as month_cost,
                       COUNT(DISTINCT tu.session_id) as month_sessions,
                       SUM(tu.input_tokens + tu.output_tokens) as month_tokens
                FROM token_usage tu
                WHERE strftime('%Y-%m', tu.timestamp) = strftime('%Y-%m', 'now'){extra}"""
        async with conn.execute(q, mp) as cur:
            current = (await cur.fetchone()) or {}

        # Daily spend this month
        q2 = f"""SELECT date(tu.timestamp) as date,
                        ROUND(SUM(tu.estimated_cost_usd), 4) as cost,
                        COUNT(DISTINCT tu.session_id) as sessions
                 FROM token_usage tu
                 WHERE strftime('%Y-%m', tu.timestamp) = strftime('%Y-%m', 'now'){extra}
                 GROUP BY date(tu.timestamp) ORDER BY date"""
        async with conn.execute(q2, mp) as cur:
            daily = await cur.fetchall()

        # Per-user breakdown this month
        q3 = f"""SELECT COALESCE(m.logged_in_user, m.machine_name, tu.machine_id) as user_name,
                        ROUND(SUM(tu.estimated_cost_usd), 4) as cost,
                        COUNT(DISTINCT tu.session_id) as sessions
                 FROM token_usage tu
                 LEFT JOIN machines m ON tu.machine_id = m.machine_id
                 WHERE strftime('%Y-%m', tu.timestamp) = strftime('%Y-%m', 'now'){extra}
                 GROUP BY user_name ORDER BY cost DESC"""
        async with conn.execute(q3, mp) as cur:
            per_user = await cur.fetchall()

        # Last 3 months for comparison
        q4 = f"""SELECT strftime('%Y-%m', tu.timestamp) as month,
                        ROUND(SUM(tu.estimated_cost_usd), 4) as cost
                 FROM token_usage tu
                 WHERE tu.timestamp >= date('now', '-90 days'){extra}
                 GROUP BY month ORDER BY month"""
        async with conn.execute(q4, mp) as cur:
            monthly_history = await cur.fetchall()

        month_cost = current.get('month_cost', 0) or 0
        month_tokens = current.get('month_tokens', 0) or 0

        # Calculate burn rate and projections. Use UTC "today" since
        # month_cost above is bucketed by strftime('%Y-%m', ...) against
        # SQLite's 'now' (also UTC) — using local server time here would
        # make days_elapsed drift out of sync with which rows actually
        # fall in "this month" near midnight UTC boundaries.
        today = datetime.utcnow()
        days_elapsed = today.day
        days_in_month = 30  # simplified
        days_remaining = max(days_in_month - days_elapsed, 0)

        daily_burn = round(month_cost / max(days_elapsed, 1), 2)
        projected_total = round(daily_burn * days_in_month, 2)
        budget_used_pct = round(month_cost / max(monthly_budget, 0.01) * 100, 1)

        if projected_total > monthly_budget:
            overshoot_day = int(monthly_budget / max(daily_burn, 0.0001))
            overshoot_date = today.replace(day=1) + timedelta(days=min(overshoot_day, days_in_month) - 1)
            overshoot_date_str = overshoot_date.strftime('%Y-%m-%d')
        else:
            overshoot_date_str = None

        status = "on_track"
        if budget_used_pct >= 100:
            status = "over_budget"
        elif budget_used_pct >= 80:
            status = "warning"
        elif projected_total > monthly_budget:
            status = "at_risk"

    return JSONResponse({
        "budget": {
            "monthly_budget": monthly_budget,
            "month_spent": month_cost,
            "budget_used_pct": budget_used_pct,
            "budget_remaining": round(monthly_budget - month_cost, 2),
            "daily_burn_rate": daily_burn,
            "projected_total": projected_total,
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "overshoot_date": overshoot_date_str,
            "status": status,
        },
        "daily_spend": daily,
        "per_user": per_user,
        "monthly_history": monthly_history,
        "month_tokens": month_tokens,
        "month_sessions": current.get('month_sessions', 0) or 0,
    })


@app.get("/api/insights/cost-efficiency")
async def api_insights_cost_efficiency(days: int = 30, machine_id: str = None):
    """Cost efficiency scores per session."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra_s, mp_s = Database._machine_scope_clause(machine_id, "s.machine_id")
        extra_tu, mp_tu = Database._machine_scope_clause(machine_id, "tu.machine_id")

        # Per-session cost efficiency
        q = f"""SELECT s.session_id, s.start_time, s.duration_seconds,
                       s.working_directory, s.model_name,
                       COALESCE(m.logged_in_user, m.machine_name) as user_name,
                       COALESCE(tu.cost, 0) as cost,
                       COALESCE(tu.input_tok, 0) as input_tokens,
                       COALESCE(tu.output_tok, 0) as output_tokens,
                       COALESCE(msg.msg_count, 0) as messages
                FROM sessions s
                LEFT JOIN machines m ON s.machine_id = m.machine_id
                LEFT JOIN (
                    SELECT session_id,
                           ROUND(SUM(estimated_cost_usd), 4) as cost,
                           SUM(input_tokens) as input_tok,
                           SUM(output_tokens) as output_tok
                    FROM token_usage GROUP BY session_id
                ) tu ON s.session_id = tu.session_id
                LEFT JOIN (
                    SELECT session_id, COUNT(*) as msg_count
                    FROM messages GROUP BY session_id
                ) msg ON s.session_id = msg.session_id
                WHERE s.start_time >= date('now', ? || ' days'){extra_s}
                AND COALESCE(tu.cost, 0) > 0
                ORDER BY tu.cost DESC"""
        async with conn.execute(q, [f"-{days}"] + mp_s) as cur:
            sessions = await cur.fetchall()

        # Calculate efficiency metrics
        scored = []
        costs = []
        rates = []
        for s in sessions:
            cost = s['cost'] or 0
            dur = s['duration_seconds'] or 0
            output = s['output_tokens'] or 0
            inp = s['input_tokens'] or 1
            msgs = s['messages'] or 0

            cost_per_min = round(cost / (dur / 60), 2) if dur > 60 else 0
            cost_per_turn = round(cost / max(msgs / 2, 1), 2)
            output_ratio = round(output / inp * 100, 1) if inp > 0 else 0

            # Efficiency score: higher output ratio + lower cost/min = better
            # Normalized 0-100 scale
            efficiency = min(100, round(output_ratio * 2 - (cost_per_min * 10), 1))
            efficiency = max(0, efficiency)

            costs.append(cost)
            if cost_per_min > 0:
                rates.append(cost_per_min)

            proj = (s['working_directory'] or '').replace('\\', '/').split('/')[-1]
            scored.append({
                "session_id": s['session_id'][:12],
                "start_time": s['start_time'],
                "project": proj,
                "user": s['user_name'],
                "model": s['model_name'],
                "cost": cost,
                "duration_min": round(dur / 60, 1) if dur > 0 else 0,
                "messages": msgs,
                "cost_per_min": cost_per_min,
                "cost_per_turn": cost_per_turn,
                "output_ratio": output_ratio,
                "efficiency_score": efficiency,
            })

        # Percentile stats
        sorted_costs = sorted(costs)
        p50 = sorted_costs[len(sorted_costs) // 2] if sorted_costs else 0
        p90 = sorted_costs[int(len(sorted_costs) * 0.9)] if sorted_costs else 0
        avg_rate = round(sum(rates) / max(len(rates), 1), 2)

        # Flag expensive sessions (>p90 or cost/min > 2x average)
        flagged = [s for s in scored if s['cost'] > p90 or (s['cost_per_min'] > avg_rate * 2 and s['cost_per_min'] > 0)]

    return JSONResponse({
        "sessions": scored[:50],
        "flagged_expensive": flagged[:10],
        "stats": {
            "total_sessions": len(scored),
            "median_cost": round(p50, 2),
            "p90_cost": round(p90, 2),
            "avg_cost_per_min": avg_rate,
            "total_flagged": len(flagged),
        },
        "cost_distribution": {
            "under_1": len([c for c in costs if c < 1]),
            "1_to_5": len([c for c in costs if 1 <= c < 5]),
            "5_to_10": len([c for c in costs if 5 <= c < 10]),
            "10_to_25": len([c for c in costs if 10 <= c < 25]),
            "over_25": len([c for c in costs if c >= 25]),
        }
    })


# ── MEDIUM-VALUE REPORTS ─────────────────────────────────────────────

@app.get("/api/insights/resource-health")
async def api_insights_resource_health(days: int = 30, machine_id: str = None):
    """Resource health: CPU/Memory trends from process_metrics."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "pm.machine_id")

        # Daily aggregates
        q = f"""SELECT date(pm.timestamp) as date,
                       ROUND(AVG(pm.cpu_percent), 1) as avg_cpu,
                       ROUND(MAX(pm.cpu_percent), 1) as peak_cpu,
                       ROUND(AVG(pm.memory_rss_mb), 0) as avg_mem_mb,
                       ROUND(MAX(pm.memory_rss_mb), 0) as peak_mem_mb,
                       ROUND(AVG(pm.num_threads), 0) as avg_threads,
                       COUNT(*) as samples
                FROM process_metrics pm
                WHERE pm.timestamp >= date('now', ? || ' days'){extra}
                GROUP BY date(pm.timestamp) ORDER BY date"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            daily = await cur.fetchall()

        # Per-session resource hogs (top 10 by peak memory)
        q2 = f"""SELECT pm.session_id,
                        ROUND(MAX(pm.cpu_percent), 1) as peak_cpu,
                        ROUND(MAX(pm.memory_rss_mb), 0) as peak_mem_mb,
                        ROUND(AVG(pm.cpu_percent), 1) as avg_cpu,
                        ROUND(AVG(pm.memory_rss_mb), 0) as avg_mem_mb,
                        COUNT(*) as samples,
                        s.working_directory, s.model_name,
                        COALESCE(m.logged_in_user, m.machine_name) as user_name
                 FROM process_metrics pm
                 JOIN sessions s ON pm.session_id = s.session_id
                 LEFT JOIN machines m ON pm.machine_id = m.machine_id
                 WHERE pm.timestamp >= date('now', ? || ' days'){extra}
                 GROUP BY pm.session_id
                 ORDER BY peak_mem_mb DESC LIMIT 10"""
        async with conn.execute(q2, [f"-{days}"] + mp) as cur:
            hogs = await cur.fetchall()

        # Overall stats
        q3 = f"""SELECT ROUND(AVG(cpu_percent), 1) as avg_cpu,
                        ROUND(MAX(cpu_percent), 1) as max_cpu,
                        ROUND(AVG(memory_rss_mb), 0) as avg_mem,
                        ROUND(MAX(memory_rss_mb), 0) as max_mem,
                        COUNT(*) as total_samples,
                        COUNT(DISTINCT session_id) as sessions_monitored
                 FROM process_metrics pm
                 WHERE pm.timestamp >= date('now', ? || ' days'){extra}"""
        async with conn.execute(q3, [f"-{days}"] + mp) as cur:
            overall = (await cur.fetchone()) or {}

    # Enrich hogs with project name
    for h in hogs:
        h['project'] = (h.get('working_directory') or '').replace('\\', '/').split('/')[-1]

    return JSONResponse({
        "daily": daily,
        "resource_hogs": hogs,
        "overall": overall,
    })


@app.get("/api/insights/context-switches")
async def api_insights_context_switches(days: int = 30, machine_id: str = None):
    """Context switch analysis: project switching per day."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "s.machine_id")

        # Daily project count and session count
        q = f"""SELECT date(s.start_time) as date,
                       COUNT(DISTINCT s.working_directory) as unique_projects,
                       COUNT(*) as sessions,
                       GROUP_CONCAT(DISTINCT
                           REPLACE(REPLACE(s.working_directory, '\\\\', '/'), '/', '/')
                       ) as projects_list
                FROM sessions s
                WHERE s.start_time >= date('now', ? || ' days'){extra}
                GROUP BY date(s.start_time)
                ORDER BY date"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            daily = await cur.fetchall()

        # Per-user switch frequency
        q2 = f"""SELECT COALESCE(m.logged_in_user, m.machine_name, s.machine_id) as user_name,
                        COUNT(DISTINCT date(s.start_time)) as active_days,
                        COUNT(DISTINCT s.working_directory) as total_projects,
                        COUNT(*) as total_sessions,
                        ROUND(AVG(day_projects), 1) as avg_projects_per_day
                 FROM sessions s
                 LEFT JOIN machines m ON s.machine_id = m.machine_id
                 LEFT JOIN (
                     SELECT date(start_time) as d, machine_id,
                            COUNT(DISTINCT working_directory) as day_projects
                     FROM sessions
                     WHERE start_time >= date('now', ? || ' days')
                     GROUP BY d, machine_id
                 ) dp ON date(s.start_time) = dp.d AND s.machine_id = dp.machine_id
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 GROUP BY user_name"""
        async with conn.execute(q2, [f"-{days}", f"-{days}"] + mp) as cur:
            per_user = await cur.fetchall()

        # Stats
        high_switch_days = len([d for d in daily if d['unique_projects'] >= 3])
        avg_projects = round(sum(d['unique_projects'] for d in daily) / max(len(daily), 1), 1)

    # Shorten project paths
    for d in daily:
        if d.get('projects_list'):
            projs = d['projects_list'].split(',')
            d['projects_short'] = [p.replace('\\', '/').split('/')[-1] for p in projs]
        else:
            d['projects_short'] = []

    return JSONResponse({
        "daily": daily,
        "per_user": per_user,
        "stats": {
            "avg_projects_per_day": avg_projects,
            "high_switch_days": high_switch_days,
            "total_days": len(daily),
        }
    })


@app.get("/api/insights/velocity-by-tag")
async def api_insights_velocity_by_tag(days: int = 28, machine_id: str = None):
    """Project-health/velocity breakdown by session tag (bugfix/feature/
    refactor/...) -- shows how much time/cost/session volume each kind of
    work is taking, week over week."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "s.machine_id")

        q = f"""SELECT t.name as tag, COUNT(DISTINCT s.session_id) as sessions,
                       ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 1) as hours,
                       ROUND(COALESCE(SUM(tu.estimated_cost_usd), 0), 4) as cost
                FROM session_tags st
                JOIN tags t ON t.id = st.tag_id
                JOIN sessions s ON s.session_id = st.session_id
                LEFT JOIN token_usage tu ON tu.session_id = s.session_id
                WHERE s.start_time >= date('now', ? || ' days'){extra}
                GROUP BY t.name ORDER BY sessions DESC"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            by_tag = await cur.fetchall()

        q2 = f"""SELECT strftime('%Y-W%W', s.start_time) as week, t.name as tag,
                        COUNT(DISTINCT s.session_id) as sessions
                 FROM session_tags st
                 JOIN tags t ON t.id = st.tag_id
                 JOIN sessions s ON s.session_id = st.session_id
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 GROUP BY week, t.name ORDER BY week"""
        async with conn.execute(q2, [f"-{days}"] + mp) as cur:
            weekly_rows = await cur.fetchall()

        # Untagged session count, so the breakdown is honest about coverage
        q3 = f"""SELECT COUNT(DISTINCT s.session_id) as untagged
                 FROM sessions s
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 AND s.session_id NOT IN (SELECT session_id FROM session_tags)"""
        async with conn.execute(q3, [f"-{days}"] + mp) as cur:
            untagged = (await cur.fetchone() or {}).get("untagged", 0)

    weeks: Dict[str, Dict[str, int]] = {}
    for row in weekly_rows:
        weeks.setdefault(row["week"], {})[row["tag"]] = row["sessions"]

    return JSONResponse({
        "by_tag": by_tag,
        "weekly": [{"week": w, **counts} for w, counts in sorted(weeks.items())],
        "untagged_sessions": untagged,
    })


@app.get("/api/insights/anomalies")
async def api_insights_anomalies(days: int = 30, machine_id: str = None):
    """Detect (and persist) cost/token/session-count spikes per machine and
    per user, then return them grouped with open/ack/dismissed status."""
    anomalies = await app.state.db.detect_anomalies(days=days, machine_id=machine_id)
    summary = {"open": 0, "acknowledged": 0, "dismissed": 0, "high": 0, "medium": 0, "low": 0}
    for a in anomalies:
        summary[a.get("status", "open")] = summary.get(a.get("status", "open"), 0) + 1
        summary[a["severity"]] = summary.get(a["severity"], 0) + 1
    return JSONResponse({"anomalies": anomalies, "summary": summary})


@app.post("/api/anomalies/{anomaly_id}/acknowledge")
async def api_acknowledge_anomaly(request: Request, anomaly_id: int):
    email = (auth_utils.current_user(request) or {}).get("email") if request.state.auth_enabled else "local"
    ok = await app.state.db.set_anomaly_status(anomaly_id, "acknowledged", by=email)
    if not ok:
        return JSONResponse({"error": "Anomaly not found"}, status_code=404)
    return JSONResponse({"status": "acknowledged"})


@app.post("/api/anomalies/{anomaly_id}/dismiss")
async def api_dismiss_anomaly(request: Request, anomaly_id: int):
    ok = await app.state.db.set_anomaly_status(anomaly_id, "dismissed")
    if not ok:
        return JSONResponse({"error": "Anomaly not found"}, status_code=404)
    return JSONResponse({"status": "dismissed"})


@app.get("/api/insights/benchmark")
async def api_insights_benchmark(days: int = 30, machine_id: str = None):
    """Cross-team and cross-user benchmarking: adoption, velocity, and cost
    compared side-by-side across groups (teams) and individual users."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra_s, mp_s = Database._machine_scope_clause(machine_id, "s.machine_id")
        extra_t, mp_t = Database._machine_scope_clause(machine_id, "tu.machine_id")

        # Per-machine session/engagement stats within the window
        q = f"""SELECT s.machine_id,
                       COUNT(*) as sessions,
                       COUNT(DISTINCT date(s.start_time)) as active_days,
                       ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 2) as hours
                FROM sessions s
                WHERE s.start_time >= date('now', ? || ' days'){extra_s}
                GROUP BY s.machine_id"""
        async with conn.execute(q, [f"-{days}"] + mp_s) as cur:
            machine_sessions = {r["machine_id"]: r for r in await cur.fetchall()}

        # Per-machine cost/tokens within the window
        q2 = f"""SELECT tu.machine_id,
                        ROUND(COALESCE(SUM(tu.estimated_cost_usd), 0), 4) as cost,
                        COALESCE(SUM(tu.input_tokens), 0) as input_tokens,
                        COALESCE(SUM(tu.output_tokens), 0) as output_tokens
                 FROM token_usage tu
                 WHERE tu.timestamp >= date('now', ? || ' days'){extra_t}
                 GROUP BY tu.machine_id"""
        async with conn.execute(q2, [f"-{days}"] + mp_t) as cur:
            machine_costs = {r["machine_id"]: r for r in await cur.fetchall()}

        # Machine -> group/user mapping (all machines, not just active ones,
        # so a team's member_count reflects its roster, not just who used
        # Claude this period)
        async with conn.execute(
            """SELECT m.machine_id, m.logged_in_user, m.machine_name, m.group_id, g.name as group_name
               FROM machines m LEFT JOIN groups g ON m.group_id = g.id"""
        ) as cur:
            machines = await cur.fetchall()

    def stats_for(machine_id_):
        s = machine_sessions.get(machine_id_, {})
        c = machine_costs.get(machine_id_, {})
        return {
            "sessions": s.get("sessions", 0) or 0,
            "active_days": s.get("active_days", 0) or 0,
            "hours": s.get("hours", 0) or 0,
            "cost": c.get("cost", 0) or 0,
            "input_tokens": c.get("input_tokens", 0) or 0,
            "output_tokens": c.get("output_tokens", 0) or 0,
        }

    def aggregate(buckets: Dict[str, dict], key: str, label: str, machine_ids: set):
        agg = buckets.setdefault(key, {
            "name": label, "machines": set(), "sessions": 0, "active_days_sum": 0,
            "hours": 0.0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
        })
        agg["machines"] |= machine_ids
        for mid in machine_ids:
            st = stats_for(mid)
            agg["sessions"] += st["sessions"]
            agg["active_days_sum"] += st["active_days"]
            agg["hours"] += st["hours"]
            agg["cost"] += st["cost"]
            agg["input_tokens"] += st["input_tokens"]
            agg["output_tokens"] += st["output_tokens"]

    teams: Dict[str, dict] = {}
    users: Dict[str, dict] = {}
    for m in machines:
        mid = m["machine_id"]
        team_key = str(m["group_id"]) if m["group_id"] else "unassigned"
        team_label = m["group_name"] or "Unassigned"
        aggregate(teams, team_key, team_label, {mid})
        user_key = m["logged_in_user"] or mid
        aggregate(users, user_key, m["logged_in_user"] or (m["machine_name"] or mid), {mid})

    def finalize(buckets: Dict[str, dict], entity_label: str):
        rows = []
        for key, agg in buckets.items():
            member_count = len(agg["machines"])
            avg_active_days = round(agg["active_days_sum"] / max(member_count, 1), 1)
            engagement_pct = round(avg_active_days / max(days, 1) * 100, 1)
            rows.append({
                "id": key,
                "name": agg["name"],
                entity_label: member_count,
                "sessions": agg["sessions"],
                "hours": round(agg["hours"], 1),
                "cost": round(agg["cost"], 2),
                "input_tokens": agg["input_tokens"],
                "output_tokens": agg["output_tokens"],
                "engagement_pct": engagement_pct,
                "cost_per_session": round(agg["cost"] / max(agg["sessions"], 1), 2),
                "sessions_per_member": round(agg["sessions"] / max(member_count, 1), 1),
                "output_per_dollar": round(agg["output_tokens"] / max(agg["cost"], 0.01), 0),
            })
        # Rank by a composite "velocity" score (sessions/member, higher better)
        rows.sort(key=lambda r: -r["sessions_per_member"])
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return rows

    return JSONResponse({
        "teams": finalize(teams, "members"),
        "users": finalize(users, "machines"),
        "days": days,
    })


@app.get("/benchmarking", response_class=HTMLResponse)
async def benchmarking_page(request: Request, machine_id: str = None):
    """Cross-team/cross-user benchmarking page."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)
    return app.state.templates.TemplateResponse(
        "benchmarking.html",
        {"request": request, "selected_machine": selected_machine, "machine_id": machine_id},
    )


@app.get("/ask", response_class=HTMLResponse)
async def ask_page(request: Request, machine_id: str = None):
    """Natural-language query page."""
    # Example chips are seeded from data that actually exists in this
    # deployment (not a hardcoded fictitious "Team Alpha") so clicking one
    # never immediately fails with "no team found".
    teams = await app.state.db.nlq_known_teams()
    return app.state.templates.TemplateResponse(
        "ask.html", {"request": request, "machine_id": machine_id, "example_team": teams[0] if teams else None},
    )


@app.post("/api/query")
async def api_nl_query(request: Request, data: dict):
    """Answer a natural-language question like 'How much did Team X spend
    last week?' using a local, deterministic keyword/regex parser (no
    external LLM call — see src/utils/nlq.py) against the existing data."""
    question = (data.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)

    machine_id = request.query_params.get("machine_id")
    teams = await app.state.db.nlq_known_teams()
    users = await app.state.db.nlq_known_users()
    parsed = parse_query(question, teams, users)

    # Resolve "me" against the authenticated caller's own username, if known.
    if parsed.scope_type == "me":
        email = (auth_utils.current_user(request) or {}).get("email") if request.state.auth_enabled else None
        resolved = (email or "").split("@")[0].lower() if email else None
        if resolved and resolved in users:
            parsed.scope_value = resolved
            parsed.scope_label = resolved
        else:
            parsed.matched = False
            parsed.unmatched_reason = "Couldn't determine who 'you' are — sign in or ask about a specific team/user instead."

    email_for_log = (auth_utils.current_user(request) or {}).get("email") if request.state.auth_enabled else "local"
    await app.state.db.log_nl_query(question, parsed, asked_by=email_for_log)

    if not parsed.matched:
        # Build examples from data that actually exists in this deployment
        # (a hardcoded "Team Alpha" would itself fail if no such team is
        # configured here, which is exactly the kind of broken suggestion
        # that erodes trust in this feature).
        examples = [
            "How many sessions did I have this month?",
            "How many tokens were used yesterday?",
            "Who is spending the most this month?",
        ]
        if teams:
            examples.insert(0, f"How much did Team {teams[0]} spend last week?")
        return JSONResponse({
            "answer": None,
            "matched": False,
            "reason": parsed.unmatched_reason,
            "examples": examples,
        })

    result = await app.state.db.run_nl_query(parsed, machine_id=machine_id)
    scope_phrase = {
        "all": "across everyone",
        "team": f"for Team {parsed.scope_label}",
        "user": f"for {parsed.scope_label}",
        "me": "for you",
    }.get(parsed.scope_type, "")
    if result.get("is_ranking"):
        scope_phrase = f"Top by {METRIC_LABELS.get(parsed.metric, parsed.metric)}, " + scope_phrase

    return JSONResponse({
        "matched": True,
        "metric": parsed.metric,
        "scope_type": parsed.scope_type,
        "scope_label": parsed.scope_label,
        "time_label": parsed.time_label,
        "scope_phrase": scope_phrase,
        "result": result,
        "no_scope_match": result.get("no_scope_match", False),
    })


# ---------------------------------------------------------------------------
# Azure DevOps integration: links sessions to PRs/commits for "AI-assisted
# PR" stats. Org URL/PAT are environment-variable only (see Config) -- the
# PAT is never accepted through this API. Mapping/sync mutations are
# restricted to admins; the resulting stats are readable by everyone like
# the rest of the insights endpoints.
# ---------------------------------------------------------------------------

@app.get("/integrations/azure-devops", response_class=HTMLResponse)
async def ado_integration_page(request: Request):
    """Azure DevOps mapping/sync admin page + AI-assisted PR stats."""
    if request.state.auth_enabled and not getattr(request.state, "is_admin", False):
        return RedirectResponse(url=f"{ROOT_PATH}/")
    return app.state.templates.TemplateResponse(
        "ado_integration.html",
        {
            "request": request,
            "ado_configured": Config.AZURE_DEVOPS_ENABLED,
            # This is the org-wide admin control plane for ADO sync/stats --
            # it intentionally has no team/machine scoping, so the global
            # selector would be misleading (it does nothing here).
            "hide_machine_topbar": True,
        },
    )


@app.get("/api/integrations/ado/status")
async def api_ado_status():
    return JSONResponse({
        "configured": Config.AZURE_DEVOPS_ENABLED,
        "org_url": Config.AZURE_DEVOPS_ORG_URL or None,
    })


@app.post("/api/integrations/ado/sync")
async def api_ado_sync(request: Request):
    """Auto-discover every project/repository in the Azure DevOps org,
    pull their PRs/commits, and re-run the session<->PR linking heuristic."""
    guard = _require_admin(request)
    if guard:
        return guard
    try:
        summary = await sync_azure_devops(app.state.db)
    except AzureDevOpsError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(summary)


@app.get("/api/integrations/ado/pull-requests")
async def api_ado_pull_requests(status: str = "all", limit: int = 100, offset: int = 0,
                                 ai_only: bool = False, machine_id: str = None,
                                 project: str = None, author: str = None,
                                 known_users_only: bool = False):
    """machine_id is optional and omitted by the admin Azure DevOps page
    (full org, unscoped). The user-facing Git Pull Requests page passes it
    through to scope results to the currently selected user(s), and always
    sets known_users_only=True so that even its unscoped 'all' view only
    shows PRs by recognized Claude Code users, never the entire ADO org."""
    return JSONResponse({"pull_requests": await app.state.db.get_ado_pull_requests(
        status=status, limit=limit, offset=offset, ai_only=ai_only, machine_id=machine_id,
        project=project, author=author, known_users_only=known_users_only,
    )})


@app.get("/api/integrations/ado/pr-projects")
async def api_ado_pr_projects():
    """Distinct Project/Repository combos across synced PRs, for the PR
    report's filter dropdown. (Not to be confused with the old, now-removed
    /api/integrations/ado/projects endpoint from the manual-mapping feature.)"""
    return JSONResponse({"projects": await app.state.db.get_ado_projects_repos()})


def _pr_code_area(path: str) -> str:
    """Bucket a changed file's path into a 'code area' (its first two path
    segments, e.g. 'src/api') for the PR detail report's AI-adoption
    breakdown by folder."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) <= 1:
        return parts[0] if parts else "(root)"
    return "/".join(parts[:2])


def _parse_pr_threads(threads: list) -> dict:
    """Best-effort extraction of reviewer feedback + approval timestamps
    from a PR's comment threads. Azure DevOps does not document a stable,
    official schema for the system "vote changed" threads it generates, so
    this looks for any thread `properties` key that mentions a vote result
    and treats its earliest comment's publishedDate as that vote's
    timestamp -- gracefully returning no timestamp if the shape doesn't
    match rather than raising. Real (human-authored) comments are reliably
    identified via the documented `commentType == "text"` field."""
    feedback = []
    vote_events = []  # [{display_name, vote, vote_label, voted_at}]
    for thread in threads or []:
        comments = thread.get("comments") or []
        if not comments:
            continue
        props = thread.get("properties") or {}
        vote_value = None
        for key, val in props.items():
            if "vote" in key.lower():
                raw = val.get("$value") if isinstance(val, dict) else val
                try:
                    vote_value = int(raw)
                except (TypeError, ValueError):
                    continue
                break
        first_comment = comments[0]
        if vote_value is not None:
            vote_events.append({
                "display_name": (first_comment.get("author") or {}).get("displayName"),
                "vote": vote_value,
                "vote_label": vote_label(vote_value),
                "voted_at": first_comment.get("publishedDate") or thread.get("publishedDate"),
            })
            continue
        for c in comments:
            if c.get("commentType") != "text" or not (c.get("content") or "").strip():
                continue
            feedback.append({
                "author": (c.get("author") or {}).get("displayName"),
                "content": c.get("content"),
                "published_date": c.get("publishedDate"),
                "is_deleted": bool(c.get("isDeleted")),
            })

    first_approval_at = None
    approving = [v["voted_at"] for v in vote_events if v["vote"] and v["vote"] >= 5 and v["voted_at"]]
    if approving:
        first_approval_at = min(approving)

    feedback.sort(key=lambda c: c.get("published_date") or "")
    return {"feedback": feedback, "vote_events": vote_events, "first_approval_at": first_approval_at}


@app.get("/api/integrations/ado/pull-requests/{pr_row_id}/details")
async def api_ado_pr_details(pr_row_id: int):
    """Detailed, commit-level AI-adoption report for a single PR: which
    commits were made during an active Claude Code session vs not, and
    (fetched live from Azure DevOps, on-demand -- see
    AzureDevOpsClient.get_commit_changes) which code areas/files each
    touched, so a reviewer can see exactly where AI was and wasn't used.
    Also includes author/co-author/reviewer/completion metadata and (best
    effort, fetched on-demand from the PR's comment threads) review
    feedback and time-to-approve."""
    pr = await app.state.db.get_ado_pr_detail(pr_row_id)
    if not pr:
        return JSONResponse({"error": "Pull request not found"}, status_code=404)

    files_error = None
    threads_error = None
    if Config.AZURE_DEVOPS_ENABLED:
        try:
            client = AzureDevOpsClient()

            async def fetch_files(commit):
                try:
                    commit["files"] = await client.get_commit_changes(
                        pr["ado_project"], pr["ado_repository"], commit["commit_sha"]
                    )
                except httpx.HTTPError as e:
                    commit["files"] = []
                    commit["files_error"] = str(e)

            async def fetch_threads():
                return await client.list_pr_threads(pr["ado_project"], pr["ado_repository"], pr["ado_pr_id"])

            results = await asyncio.gather(
                asyncio.gather(*(fetch_files(c) for c in pr["commits"])),
                fetch_threads(),
                return_exceptions=True,
            )
            threads = []
            if isinstance(results[1], Exception):
                threads_error = str(results[1])
            else:
                threads = results[1]
            thread_info = _parse_pr_threads(threads)
        except AzureDevOpsError as e:
            files_error = str(e)
            threads_error = str(e)
            thread_info = {"feedback": [], "vote_events": [], "first_approval_at": None}
            for c in pr["commits"]:
                c["files"] = []
    else:
        files_error = "Azure DevOps is not configured"
        threads_error = files_error
        thread_info = {"feedback": [], "vote_events": [], "first_approval_at": None}
        for c in pr["commits"]:
            c["files"] = []

    # Aggregate per-code-area AI adoption: which folders were touched only
    # by AI-assisted commits, only by manual commits, or both.
    area_map: Dict[str, Dict[str, set]] = {}
    for c in pr["commits"]:
        bucket = "ai" if c["is_ai"] else "manual"
        for f in c.get("files", []):
            area = _pr_code_area(f["path"])
            area_map.setdefault(area, {"ai": set(), "manual": set()})
            area_map[area][bucket].add(f["path"])

    code_areas = []
    for area, buckets in sorted(area_map.items()):
        ai_files, manual_files = buckets["ai"], buckets["manual"]
        code_areas.append({
            "area": area,
            "total_files": len(ai_files | manual_files),
            "ai_only_files": sorted(ai_files - manual_files),
            "manual_only_files": sorted(manual_files - ai_files),
            "both_files": sorted(ai_files & manual_files),
        })
    code_areas.sort(key=lambda a: a["total_files"], reverse=True)

    pr["code_areas"] = code_areas
    pr["files_error"] = files_error
    pr["feedback"] = thread_info["feedback"]
    pr["vote_events"] = thread_info["vote_events"]
    pr["threads_error"] = threads_error
    pr["time_to_approve_hours"] = Database._hours_between(pr.get("created_date"), thread_info["first_approval_at"])
    return JSONResponse(pr)


@app.get("/api/integrations/ado/ai-assisted-stats")
async def api_ado_ai_assisted_stats(days: int = 90, machine_id: str = None, known_users_only: bool = False):
    return JSONResponse(await app.state.db.get_ai_assisted_pr_stats(
        days=days, machine_id=machine_id, known_users_only=known_users_only,
    ))


@app.get("/git-pull-requests", response_class=HTMLResponse)
async def git_pull_requests_page(request: Request, machine_id: str = None):
    """User-facing Git Pull Requests page (Monitor section, available to
    everyone): same AI-assisted PR stats/table as the Azure DevOps admin
    page, but scoped to the currently selected user/machine (or 'all'
    Claude Code users if none selected) instead of the entire ADO org."""
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)
    return app.state.templates.TemplateResponse(
        "git_pull_requests.html",
        {
            "request": request,
            "ado_configured": Config.AZURE_DEVOPS_ENABLED,
            "selected_machine": selected_machine,
            "machine_id": machine_id,
        },
    )


@app.get("/api/insights/dev-velocity")
async def api_insights_dev_velocity(days: int = 28, machine_id: str = None):
    """Developer velocity: week-over-week metrics with trend arrows."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra_s, mp_s = Database._machine_scope_clause(machine_id, "s.machine_id")
        extra_t, mp_t = Database._machine_scope_clause(machine_id, "tu.machine_id")

        # Weekly sessions + duration
        q = f"""SELECT strftime('%Y-W%W', s.start_time) as week,
                       COUNT(*) as sessions,
                       ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 1) as hours,
                       COUNT(DISTINCT s.working_directory) as projects
                FROM sessions s
                WHERE s.start_time >= date('now', ? || ' days'){extra_s}
                GROUP BY week ORDER BY week"""
        async with conn.execute(q, [f"-{days}"] + mp_s) as cur:
            weekly_sessions = await cur.fetchall()

        # Weekly cost
        q3 = f"""SELECT strftime('%Y-W%W', tu.timestamp) as week,
                        ROUND(SUM(tu.estimated_cost_usd), 4) as cost,
                        SUM(tu.output_tokens) as output_tokens
                 FROM token_usage tu
                 WHERE tu.timestamp >= date('now', ? || ' days'){extra_t}
                 GROUP BY week ORDER BY week"""
        async with conn.execute(q3, [f"-{days}"] + mp_t) as cur:
            weekly_cost = await cur.fetchall()

    # Merge into weekly summary
    weeks = {}
    for row in weekly_sessions:
        weeks.setdefault(row['week'], {})['sessions'] = row['sessions']
        weeks[row['week']]['hours'] = row['hours']
        weeks[row['week']]['projects'] = row['projects']
    for row in weekly_cost:
        weeks.setdefault(row['week'], {})['cost'] = row['cost']
        weeks[row['week']]['output_tokens'] = row['output_tokens']

    sorted_weeks = sorted(weeks.items())
    result = []
    prev = None
    for week, data in sorted_weeks:
        entry = {"week": week, **{k: data.get(k, 0) for k in ['sessions', 'hours', 'projects', 'cost', 'output_tokens']}}
        # Calculate trends vs previous week
        if prev:
            for key in ['sessions', 'projects', 'cost']:
                p = prev.get(key, 0) or 0
                c = entry.get(key, 0) or 0
                change = round(((c - p) / max(p, 1)) * 100, 0)
                entry[f'{key}_trend'] = change
        result.append(entry)
        prev = entry

    return JSONResponse({"weeks": result})


@app.get("/api/insights/adoption")
async def api_insights_adoption(days: int = 90, machine_id: str = None):
    """Adoption & engagement: session frequency and gaps per machine."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "s.machine_id")

        # Per-machine engagement stats
        q = f"""SELECT s.machine_id,
                       COALESCE(m.logged_in_user, m.machine_name) as user_name,
                       COUNT(*) as total_sessions,
                       COUNT(DISTINCT date(s.start_time)) as active_days,
                       MIN(s.start_time) as first_session,
                       MAX(s.start_time) as last_session,
                       ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 1) as total_hours,
                       ROUND(COALESCE(AVG(s.duration_seconds), 0) / 60.0, 1) as avg_session_min
                FROM sessions s
                LEFT JOIN machines m ON s.machine_id = m.machine_id
                WHERE s.start_time >= date('now', ? || ' days'){extra}
                GROUP BY s.machine_id"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            machines = await cur.fetchall()

        # Session gap analysis (time between consecutive sessions)
        extra_raw, mp_raw = Database._machine_scope_clause(machine_id, "machine_id")
        q2 = f"""SELECT machine_id, gap_hours FROM (
                    SELECT machine_id,
                           ROUND((julianday(start_time) - julianday(
                               LAG(start_time) OVER (PARTITION BY machine_id ORDER BY start_time)
                           )) * 24, 1) as gap_hours
                    FROM sessions
                    WHERE start_time >= date('now', ? || ' days'){extra_raw}
                    AND start_time IS NOT NULL
                 ) WHERE gap_hours IS NOT NULL"""
        async with conn.execute(q2, [f"-{days}"] + mp_raw) as cur:
            gaps = await cur.fetchall()

        # Daily session count trend (for adoption curve)
        q3 = f"""SELECT date(s.start_time) as date,
                        COUNT(*) as sessions,
                        COUNT(DISTINCT s.machine_id) as active_machines
                 FROM sessions s
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 GROUP BY date(s.start_time) ORDER BY date"""
        async with conn.execute(q3, [f"-{days}"] + mp) as cur:
            adoption_curve = await cur.fetchall()

    # Aggregate gaps per machine
    gap_stats = {}
    for g in gaps:
        mid = g['machine_id']
        gap_stats.setdefault(mid, []).append(g['gap_hours'])

    for m in machines:
        mid = m['machine_id']
        mg = gap_stats.get(mid, [])
        m['avg_gap_hours'] = round(sum(mg) / max(len(mg), 1), 1) if mg else 0
        m['min_gap_hours'] = round(min(mg), 1) if mg else 0
        m['max_gap_hours'] = round(max(mg), 1) if mg else 0
        total_possible = days
        m['engagement_pct'] = round(m['active_days'] / max(total_possible, 1) * 100, 1)
        m['sessions_per_active_day'] = round(m['total_sessions'] / max(m['active_days'], 1), 1)

    return JSONResponse({
        "machines": machines,
        "adoption_curve": adoption_curve,
    })


@app.get("/api/insights/exit-analysis")
async def api_insights_exit_analysis(days: int = 90, machine_id: str = None):
    """Session exit analysis: success/error/crash distribution."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "s.machine_id")

        # Exit code distribution
        q = f"""SELECT
                    CASE
                        WHEN s.exit_code IS NULL THEN 'Unknown'
                        WHEN s.exit_code = 0 THEN 'Success (0)'
                        WHEN s.exit_code = 1 THEN 'Error (1)'
                        WHEN s.exit_code = 2 THEN 'Misuse (2)'
                        WHEN s.exit_code BETWEEN 126 AND 128 THEN 'Cannot Execute'
                        WHEN s.exit_code > 128 THEN 'Signal (' || (s.exit_code - 128) || ')'
                        ELSE 'Other (' || s.exit_code || ')'
                    END as exit_category,
                    s.exit_code,
                    COUNT(*) as count
                FROM sessions s
                WHERE s.start_time >= date('now', ? || ' days'){extra}
                GROUP BY exit_category
                ORDER BY count DESC"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            distribution = await cur.fetchall()

        # Error sessions with details
        q2 = f"""SELECT s.session_id, s.start_time, s.exit_code,
                        s.working_directory, s.model_name, s.duration_seconds,
                        COALESCE(m.logged_in_user, m.machine_name) as user_name
                 FROM sessions s
                 LEFT JOIN machines m ON s.machine_id = m.machine_id
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 AND s.exit_code IS NOT NULL AND s.exit_code != 0
                 ORDER BY s.start_time DESC LIMIT 20"""
        async with conn.execute(q2, [f"-{days}"] + mp) as cur:
            errors = await cur.fetchall()

        # Daily success rate
        q3 = f"""SELECT date(s.start_time) as date,
                        COUNT(*) as total,
                        SUM(CASE WHEN s.exit_code = 0 THEN 1 ELSE 0 END) as success,
                        SUM(CASE WHEN s.exit_code IS NOT NULL AND s.exit_code != 0 THEN 1 ELSE 0 END) as failed
                 FROM sessions s
                 WHERE s.start_time >= date('now', ? || ' days'){extra}
                 AND s.exit_code IS NOT NULL
                 GROUP BY date(s.start_time) ORDER BY date"""
        async with conn.execute(q3, [f"-{days}"] + mp) as cur:
            daily_rate = await cur.fetchall()

    total = sum(d['count'] for d in distribution)
    success = sum(d['count'] for d in distribution if d.get('exit_code') == 0)
    success_rate = round(success / max(total, 1) * 100, 1)

    for e in errors:
        e['project'] = (e.get('working_directory') or '').replace('\\', '/').split('/')[-1]

    return JSONResponse({
        "distribution": distribution,
        "error_sessions": errors,
        "daily_success_rate": daily_rate,
        "stats": {
            "total_sessions": total,
            "success_count": success,
            "success_rate": success_rate,
            "error_count": total - success,
        }
    })


# ── NICE-TO-HAVE REPORTS ─────────────────────────────────────────────

@app.get("/api/insights/conversation-depth")
async def api_insights_conversation_depth(days: int = 30, machine_id: str = None):
    """Conversation depth: categorize sessions by turn count."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra_s, mp_s = Database._machine_scope_clause(machine_id, "s.machine_id")

        q = f"""SELECT s.session_id, s.start_time, s.duration_seconds,
                       s.working_directory, s.model_name,
                       COALESCE(m.logged_in_user, m.machine_name) as user_name,
                       COUNT(msg.id) as total_messages,
                       SUM(CASE WHEN msg.role = 'user' THEN 1 ELSE 0 END) as user_msgs,
                       SUM(CASE WHEN msg.role = 'assistant' THEN 1 ELSE 0 END) as assistant_msgs,
                       ROUND(AVG(CASE WHEN msg.role = 'assistant' THEN LENGTH(msg.content) END), 0) as avg_response_len
                FROM sessions s
                LEFT JOIN messages msg ON s.session_id = msg.session_id
                LEFT JOIN machines m ON s.machine_id = m.machine_id
                WHERE s.start_time >= date('now', ? || ' days'){extra_s}
                GROUP BY s.session_id
                ORDER BY total_messages DESC"""
        async with conn.execute(q, [f"-{days}"] + mp_s) as cur:
            sessions = await cur.fetchall()

    categories = {"quick_fix": [], "standard": [], "deep_dive": [], "exploration": []}
    for s in sessions:
        turns = (s['user_msgs'] or 0)
        proj = (s.get('working_directory') or '').replace('\\', '/').split('/')[-1]
        entry = {
            "session_id": s['session_id'][:12],
            "start_time": s['start_time'],
            "project": proj,
            "user": (s.get('user_name') or '').split('<')[0].strip(),
            "model": s['model_name'],
            "turns": turns,
            "messages": s['total_messages'] or 0,
            "duration_min": round((s['duration_seconds'] or 0) / 60, 1),
            "avg_response_len": s['avg_response_len'] or 0,
        }
        if turns <= 2:
            entry['category'] = 'Quick Fix'
            categories['quick_fix'].append(entry)
        elif turns <= 5:
            entry['category'] = 'Standard'
            categories['standard'].append(entry)
        elif turns <= 10:
            entry['category'] = 'Deep Dive'
            categories['deep_dive'].append(entry)
        else:
            entry['category'] = 'Exploration'
            categories['exploration'].append(entry)

    distribution = {
        "Quick Fix (1-2)": len(categories['quick_fix']),
        "Standard (3-5)": len(categories['standard']),
        "Deep Dive (6-10)": len(categories['deep_dive']),
        "Exploration (10+)": len(categories['exploration']),
    }
    total = sum(distribution.values())
    all_entries = categories['quick_fix'] + categories['standard'] + categories['deep_dive'] + categories['exploration']
    avg_turns = round(sum(e['turns'] for e in all_entries) / max(total, 1), 1)
    avg_duration = round(sum(e['duration_min'] for e in all_entries) / max(total, 1), 1)

    # Top deep sessions
    deep_sessions = sorted(
        categories['deep_dive'] + categories['exploration'],
        key=lambda x: -x['turns']
    )[:15]

    return JSONResponse({
        "distribution": distribution,
        "deep_sessions": deep_sessions,
        "stats": {
            "total": total,
            "avg_turns": avg_turns,
            "avg_duration_min": avg_duration,
            "deepest_turns": max((e['turns'] for e in all_entries), default=0),
        }
    })


@app.get("/api/insights/working-hours")
async def api_insights_working_hours(days: int = 30, machine_id: str = None):
    """Working hours heatmap per developer with after-hours flagging."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "s.machine_id")

        # Per-user, per-hour, per-weekday
        q = f"""SELECT COALESCE(m.logged_in_user, m.machine_name, s.machine_id) as user_name,
                       CAST(strftime('%w', s.start_time) AS INT) as dow,
                       CAST(strftime('%H', s.start_time) AS INT) as hour,
                       COUNT(*) as sessions
                FROM sessions s
                LEFT JOIN machines m ON s.machine_id = m.machine_id
                WHERE s.start_time >= date('now', ? || ' days'){extra}
                GROUP BY user_name, dow, hour"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            data = await cur.fetchall()

    # Build per-user heatmaps
    users = {}
    for row in data:
        name = (row['user_name'] or 'Unknown').split('<')[0].strip()
        users.setdefault(name, {"matrix": [[0]*24 for _ in range(7)], "total": 0, "after_hours": 0, "weekend": 0})
        users[name]['matrix'][row['dow']][row['hour']] += row['sessions']
        users[name]['total'] += row['sessions']
        # After hours: before 8am or after 7pm on weekdays
        if row['dow'] in (0, 6):  # Sunday=0, Saturday=6
            users[name]['weekend'] += row['sessions']
        elif row['hour'] < 8 or row['hour'] >= 19:
            users[name]['after_hours'] += row['sessions']

    result = []
    for name, info in users.items():
        after_pct = round(info['after_hours'] / max(info['total'], 1) * 100, 1)
        weekend_pct = round(info['weekend'] / max(info['total'], 1) * 100, 1)
        result.append({
            "user": name,
            "matrix": info['matrix'],
            "total_sessions": info['total'],
            "after_hours_count": info['after_hours'],
            "after_hours_pct": after_pct,
            "weekend_count": info['weekend'],
            "weekend_pct": weekend_pct,
            "burnout_risk": "high" if after_pct > 30 or weekend_pct > 20 else "medium" if after_pct > 15 or weekend_pct > 10 else "low",
        })

    return JSONResponse({"users": result})


@app.get("/api/insights/machine-comparison")
async def api_insights_machine_comparison(days: int = 30, machine_id: str = None):
    """Multi-machine side-by-side comparison."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

        # Per-machine metrics
        q = """SELECT s.machine_id,
                      COALESCE(m.logged_in_user, m.machine_name) as user_name,
                      m.os_type,
                      COUNT(*) as sessions,
                      COUNT(DISTINCT date(s.start_time)) as active_days,
                      COUNT(DISTINCT s.working_directory) as projects,
                      ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 1) as total_hours,
                      ROUND(COALESCE(AVG(s.duration_seconds), 0) / 60.0, 1) as avg_session_min
               FROM sessions s
               LEFT JOIN machines m ON s.machine_id = m.machine_id
               WHERE s.start_time >= date('now', ? || ' days')"""
        frag, params = Database._machine_scope_clause(machine_id, "s.machine_id")
        q += frag + " GROUP BY s.machine_id"
        async with conn.execute(q, [f"-{days}"] + params) as cur:
            machines = await cur.fetchall()

        # Per-machine cost and tokens
        q2 = """SELECT tu.machine_id,
                       ROUND(SUM(tu.estimated_cost_usd), 4) as total_cost,
                       SUM(tu.input_tokens) as input_tokens,
                       SUM(tu.output_tokens) as output_tokens,
                       COUNT(*) as api_calls
                FROM token_usage tu
                WHERE tu.timestamp >= date('now', ? || ' days')"""
        frag, params = Database._machine_scope_clause(machine_id, "tu.machine_id")
        q2 += frag + " GROUP BY tu.machine_id"
        async with conn.execute(q2, [f"-{days}"] + params) as cur:
            costs = {r['machine_id']: r for r in await cur.fetchall()}

        # Per-machine model preference
        q3 = """SELECT tu.machine_id, tu.model_name,
                       COUNT(*) as calls,
                       ROUND(SUM(tu.estimated_cost_usd), 4) as cost
                FROM token_usage tu
                WHERE tu.timestamp >= date('now', ? || ' days')"""
        q3 += frag + " GROUP BY tu.machine_id, tu.model_name ORDER BY calls DESC"
        async with conn.execute(q3, [f"-{days}"] + params) as cur:
            model_data = await cur.fetchall()

    # Build model preferences per machine
    model_prefs = {}
    for row in model_data:
        mid = row['machine_id']
        model_prefs.setdefault(mid, []).append({
            "model": row['model_name'],
            "calls": row['calls'],
            "cost": row['cost'],
        })

    result = []
    for m in machines:
        mid = m['machine_id']
        c = costs.get(mid, {})
        total_cost = c.get('total_cost', 0) or 0
        inp = c.get('input_tokens', 0) or 0
        out = c.get('output_tokens', 0) or 0
        result.append({
            **m,
            "total_cost": total_cost,
            "input_tokens": inp,
            "output_tokens": out,
            "api_calls": c.get('api_calls', 0) or 0,
            "output_efficiency": round(out / max(inp, 1) * 100, 1),
            "cost_per_session": round(total_cost / max(m['sessions'], 1), 2),
            "cost_per_hour": round(total_cost / max(m['total_hours'], 0.1), 2),
            "model_preferences": model_prefs.get(mid, [])[:5],
        })

    return JSONResponse({"machines": result})


@app.get("/api/insights/response-size-trends")
async def api_insights_response_size_trends(days: int = 30, machine_id: str = None):
    """Response size trends: track if AI responses are growing (context bloat)."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        extra, mp = Database._machine_scope_clause(machine_id, "msg.machine_id")

        # Daily avg response length (assistant messages)
        q = f"""SELECT date(msg.timestamp) as date,
                       ROUND(AVG(LENGTH(msg.content)), 0) as avg_response_len,
                       ROUND(MAX(LENGTH(msg.content)), 0) as max_response_len,
                       COUNT(*) as response_count,
                       ROUND(AVG(msg.tokens), 0) as avg_tokens
                FROM messages msg
                WHERE msg.role = 'assistant'
                AND msg.content IS NOT NULL
                AND msg.timestamp >= date('now', ? || ' days'){extra}
                GROUP BY date(msg.timestamp)
                ORDER BY date"""
        async with conn.execute(q, [f"-{days}"] + mp) as cur:
            daily = await cur.fetchall()

        # Per-model response size
        q2 = f"""SELECT msg.model as model_name,
                        ROUND(AVG(LENGTH(msg.content)), 0) as avg_len,
                        ROUND(MAX(LENGTH(msg.content)), 0) as max_len,
                        COUNT(*) as count,
                        ROUND(AVG(msg.tokens), 0) as avg_tokens
                 FROM messages msg
                 WHERE msg.role = 'assistant'
                 AND msg.content IS NOT NULL
                 AND msg.timestamp >= date('now', ? || ' days'){extra}
                 GROUP BY msg.model
                 ORDER BY avg_len DESC"""
        async with conn.execute(q2, [f"-{days}"] + mp) as cur:
            per_model = await cur.fetchall()

        # User prompt length trends (for comparison)
        q3 = f"""SELECT date(msg.timestamp) as date,
                        ROUND(AVG(LENGTH(msg.content)), 0) as avg_prompt_len,
                        COUNT(*) as prompt_count
                 FROM messages msg
                 WHERE msg.role = 'user'
                 AND msg.content IS NOT NULL
                 AND msg.timestamp >= date('now', ? || ' days'){extra}
                 GROUP BY date(msg.timestamp)
                 ORDER BY date"""
        async with conn.execute(q3, [f"-{days}"] + mp) as cur:
            prompt_daily = await cur.fetchall()

    # Trend detection: compare first half vs second half
    if len(daily) >= 4:
        mid = len(daily) // 2
        first_half = sum(d['avg_response_len'] or 0 for d in daily[:mid]) / mid
        second_half = sum(d['avg_response_len'] or 0 for d in daily[mid:]) / (len(daily) - mid)
        trend_pct = round(((second_half - first_half) / max(first_half, 1)) * 100, 1)
        trend = "growing" if trend_pct > 10 else "shrinking" if trend_pct < -10 else "stable"
    else:
        trend_pct = 0
        trend = "insufficient_data"

    overall_avg = round(sum(d['avg_response_len'] or 0 for d in daily) / max(len(daily), 1), 0)

    return JSONResponse({
        "daily_response": daily,
        "daily_prompt": prompt_daily,
        "per_model": per_model,
        "trend": {
            "direction": trend,
            "change_pct": trend_pct,
            "overall_avg_chars": overall_avg,
        }
    })


@app.get("/api/sessions/active")
async def api_sessions_active(machine_id: str = None):
    """Get active sessions, optionally filtered by machine."""
    sessions = await app.state.db.get_active_sessions(machine_id=machine_id)
    return JSONResponse(sessions)


@app.get("/api/sessions/recent")
async def api_sessions_recent(limit: int = 100, machine_id: str = None, tag: str = None):
    """Get recent sessions, optionally filtered by machine and/or tag name."""
    sessions = await app.state.db.get_recent_sessions(limit=limit, machine_id=machine_id)
    tags_by_session = await app.state.db.get_tags_for_sessions([s["session_id"] for s in sessions])
    for s in sessions:
        s["tags"] = tags_by_session.get(s["session_id"], [])
    if tag:
        tag = tag.strip().lower()
        sessions = [s for s in sessions if any(t["name"] == tag for t in s["tags"])]
    return JSONResponse(sessions)


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request, machine_id: str = None):
    """List all sessions with links to view details."""
    # Get sessions with machine info, optionally filtered by machine
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([col[0] for col in cursor.description], row)
        )
        frag, fparams = Database._machine_scope_clause(machine_id, "s.machine_id")
        async with conn.execute(
            f"""SELECT s.*, m.machine_name, m.logged_in_user
               FROM sessions s
               LEFT JOIN machines m ON s.machine_id = m.machine_id
               WHERE 1=1{frag}
               ORDER BY s.start_time DESC
               LIMIT 200""",
            fparams
        ) as cursor:
            sessions = await cursor.fetchall()

    # Get machine details if filtering on a single machine
    selected_machine = None
    if machine_id and "," not in machine_id and machine_id != "all":
        selected_machine = await app.state.db.get_machine_by_id(machine_id)

    # Add cost information to each session
    async with app.state.db.get_connection() as conn:
        for session in sessions:
            async with conn.execute(
                """SELECT SUM(estimated_cost_usd) as cost
                   FROM token_usage WHERE session_id = ?""",
                (session['session_id'],)
            ) as cursor:
                row = await cursor.fetchone()
                session['cost'] = row[0] if row and row[0] else 0

        # Get total cost - filtered by machine/group scope if specified
        cost_frag, cost_params = Database._machine_scope_clause(machine_id, "s.machine_id")
        async with conn.execute(
            f"""SELECT SUM(tu.estimated_cost_usd)
               FROM token_usage tu
               JOIN sessions s ON tu.session_id = s.session_id
               WHERE 1=1{cost_frag}""",
            cost_params
        ) as cursor:
            total_cost = (await cursor.fetchone())[0] or 0

    tags_by_session = await app.state.db.get_tags_for_sessions([s["session_id"] for s in sessions])
    for session in sessions:
        session["tags"] = tags_by_session.get(session["session_id"], [])
    all_tags = await app.state.db.get_all_tags()

    return app.state.templates.TemplateResponse(
        "sessions_list.html",
        {
            "request": request,
            "sessions": sessions,
            "total_cost": total_cost,
            "selected_machine": selected_machine,
            "machine_id": machine_id,
            "all_tags": all_tags,
        }
    )


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "version": "1.0.0"
    })


# ============================================================================
# LEARNING & DEVELOPMENT API ENDPOINTS
# ============================================================================

@app.get("/api/learning/best-practices")
async def api_get_best_practices(category: str = None, difficulty: str = None, limit: int = Query(50, ge=0), machine_id: str = None):
    """Get best practices — when a specific machine is selected, derive real
    examples from that machine's own analyzed prompts (prompt_analysis)
    instead of the generic catalog. Falls back to the curated reference
    catalog when no machine is selected ('all') or that machine has no
    analyzed prompts yet."""
    practices = []
    if machine_id and machine_id != "all":
        practices = await app.state.db.get_derived_best_practices(machine_id, limit=limit)
    if not practices:
        practices = await app.state.db.get_best_practices(category=category, difficulty=difficulty, limit=limit)
    return JSONResponse(practices)

@app.post("/api/learning/best-practices")
async def api_create_best_practice(data: dict):
    """Create a new best practice."""
    practice_id = await app.state.db.create_best_practice(
        category=data['category'],
        title=data['title'],
        description=data['description'],
        example_prompt=data.get('example_prompt'),
        example_code=data.get('example_code'),
        tags=data.get('tags'),
        difficulty_level=data.get('difficulty_level', 'intermediate')
    )
    return JSONResponse({"status": "created", "id": practice_id})

@app.get("/api/learning/skills/{user_id}")
async def api_get_user_skills(request: Request, user_id: str):
    """Get user skills progression, derived live from real session/prompt/
    code-modification/cache activity for this user/machine."""
    denied = _check_machine_scope(request, user_id)
    if denied:
        return denied
    await app.state.db.recompute_user_skills(user_id)
    skills = await app.state.db.get_user_skills(user_id)
    return JSONResponse(skills)

@app.get("/api/learning/paths")
async def api_get_learning_paths(difficulty: str = None):
    """Get available learning paths."""
    paths = await app.state.db.get_learning_paths(difficulty=difficulty)
    return JSONResponse(paths)

@app.get("/api/learning/progress/{user_id}")
async def api_get_learning_progress(request: Request, user_id: str):
    """Get user's learning path progress, derived live from real activity."""
    denied = _check_machine_scope(request, user_id)
    if denied:
        return denied
    await app.state.db.recompute_learning_path_progress(user_id)
    progress = await app.state.db.get_user_learning_progress(user_id)
    return JSONResponse(progress)

@app.get("/api/learning/community-patterns")
async def api_get_community_patterns(pattern_type: str = None, language: str = None, limit: int = 50):
    """Get community shared patterns."""
    patterns = await app.state.db.get_community_patterns(pattern_type=pattern_type, language=language, limit=limit)
    return JSONResponse(patterns)

@app.get("/api/learning/achievements/{user_id}")
async def api_get_user_achievements(request: Request, user_id: str):
    """Get user achievements, derived live from real activity stats."""
    denied = _check_machine_scope(request, user_id)
    if denied:
        return denied
    await app.state.db.recompute_user_achievements(user_id)
    achievements = await app.state.db.get_user_achievements(user_id)
    return JSONResponse(achievements)


@app.get("/api/learning/leaderboard")
async def api_get_leaderboard(machine_id: str = None):
    """All-machines scoreboard: every machine ranked by achievement points,
    badges earned, and skill level - derived live from real activity.
    `machine_id` may be omitted/'all' (rank everyone) or a Team's resolved
    comma-separated machine list (rank just that team)."""
    leaderboard = await app.state.db.get_leaderboard(machine_id=machine_id)
    return JSONResponse(leaderboard)


@app.get("/api/learning/leaderboard/details")
async def api_get_leaderboard_details(machine_id: str = None):
    """Drill-down behind one machine's leaderboard badges: every
    achievement (earned or still locked) with the real, live activity
    number behind it and a plain-English explanation - "how did I get
    this badge / what do I need for the next one" transparency. Open to
    every authenticated user, same as /leaderboard itself; non-admins can
    only ever end up requesting their own machine(s) - enforced upstream
    by `enforce_auth`, same as every other machine_id-scoped
    route. Falls back to the first machine if `machine_id` resolves to
    more than one (a user with multiple registered devices, or an admin
    clicking a row from a Team-filtered board) - same accepted limitation
    as get_tone_details()."""
    if not machine_id or machine_id == "all":
        return JSONResponse(
            {"detail": "machine_id must identify a machine."},
            status_code=400,
        )
    single_id = machine_id.split(",")[0].strip()
    details = await app.state.db.get_achievement_details(machine_id=single_id)
    return JSONResponse(details)


@app.get("/api/tone/analysis")
async def api_get_tone_analysis(request: Request, machine_id: str = None):
    """Per-machine tone-of-conversation scoreboard: profanity, politeness,
    spelling, and grammar signals computed live from each machine's own
    chat prompts. `machine_id` follows the same None/'all'/CSV convention
    as every other machine-scoped endpoint. Restricted to the super_users
    allowlist, same as the /tone page itself."""
    denied = _require_super_user(request)
    if denied:
        return denied
    tone = await app.state.db.get_tone_analysis(machine_id=machine_id)
    return JSONResponse(tone)


@app.get("/api/tone/details")
async def api_get_tone_details(request: Request, machine_id: str = None):
    """Drill-down behind one machine's tone summary: the actual flagged
    messages/words behind each count, for "why was I scored this way"
    transparency. Restricted to the super_users allowlist, same as
    /api/tone/analysis. `enforce_auth` guarantees non-admins can
    only ever end up requesting their own machine(s); if that resolves to
    more than one machine (a user with multiple registered devices), we
    fall back to the first one rather than erroring - same "no precise
    single-machine selection for multi-machine users" limitation already
    accepted elsewhere in this app (see leaderboard_page's
    `selected_machine`)."""
    denied = _require_super_user(request)
    if denied:
        return denied
    if not machine_id or machine_id == "all":
        return JSONResponse(
            {"detail": "machine_id must identify a machine."},
            status_code=400,
        )
    single_id = machine_id.split(",")[0].strip()
    details = await app.state.db.get_tone_details(machine_id=single_id)
    return JSONResponse(details)


# ============================================================================
# AI USAGE OPTIMIZATION API ENDPOINTS
# ============================================================================

@app.get("/api/optimization/prompt-analysis")
async def api_get_prompt_analysis(session_id: str = None, min_quality: float = None, limit: int = 50, machine_id: str = None):
    """Get prompt quality analysis."""
    analysis = await app.state.db.get_prompt_analysis(session_id=session_id, min_quality=min_quality, limit=limit, machine_id=machine_id)
    return JSONResponse(analysis)

@app.post("/api/optimization/prompt-analysis")
async def api_create_prompt_analysis(data: dict):
    """Create prompt analysis."""
    if await app.state.db.prompt_analysis_exists(data['session_id'], data['prompt_text']):
        return JSONResponse({"status": "already_exists"})
    analysis_id = await app.state.db.create_prompt_analysis(
        session_id=data['session_id'],
        prompt_text=data['prompt_text'],
        **{k: v for k, v in data.items() if k not in ['session_id', 'prompt_text']}
    )
    return JSONResponse({"status": "created", "id": analysis_id})

@app.get("/api/optimization/context-insights")
async def api_get_context_insights(session_id: str = None, days: int = 30, machine_id: str = None):
    """Get context optimization insights."""
    insights = await app.state.db.get_context_insights(session_id=session_id, days=days, machine_id=machine_id)
    return JSONResponse(insights)

@app.post("/api/optimization/context-insights")
async def api_create_context_insight(data: dict):
    """Create context insight."""
    insight_id = await app.state.db.create_context_insight(
        session_id=data['session_id'],
        **{k: v for k, v in data.items() if k != 'session_id'}
    )
    return JSONResponse({"status": "created", "id": insight_id})

@app.get("/api/optimization/cache-analytics")
async def api_get_cache_analytics(session_id: str = None, days: int = 30, machine_id: str = None):
    """Get cache analytics."""
    analytics = await app.state.db.get_cache_analytics(session_id=session_id, days=days, machine_id=machine_id)
    return JSONResponse(analytics)

@app.post("/api/optimization/cache-analytics")
async def api_create_cache_analytics(data: dict):
    """Create cache analytics."""
    if await app.state.db.cache_analytics_exists(data['session_id']):
        return JSONResponse({"status": "already_exists"})
    analytics_id = await app.state.db.create_cache_analytics(
        session_id=data['session_id'],
        **{k: v for k, v in data.items() if k != 'session_id'}
    )
    return JSONResponse({"status": "created", "id": analytics_id})

@app.get("/api/optimization/model-performance")
async def api_get_model_performance(model_name: str = None, task_type: str = None, days: int = 30, machine_id: str = None):
    """Get model performance metrics, optionally scoped to one machine."""
    performance = await app.state.db.get_model_performance(model_name=model_name, task_type=task_type, days=days, machine_id=machine_id)
    return JSONResponse(performance)

@app.post("/api/optimization/model-performance")
async def api_report_model_performance(data: dict):
    """Report one completed task's real outcome for a model. Aggregated
    incrementally into that day's (machine_id, model_name, task_type) rolling
    average rather than overwritten, so multiple reports per day accumulate."""
    await app.state.db.upsert_model_performance(
        model_name=data['model_name'],
        task_type=data.get('task_type', 'general'),
        date_val=data['date'],
        response_time_ms=data.get('average_response_time_ms'),
        success=data.get('success', True),
        token_efficiency=data.get('average_token_efficiency'),
        cost=data.get('cost_per_task', 0.0),
        machine_id=data.get('machine_id')
    )
    return JSONResponse({"status": "recorded"})

@app.get("/api/optimization/token-waste")
async def api_get_token_waste_patterns(days: int = 30, machine_id: str = None):
    """Get token waste patterns, optionally scoped to one machine."""
    patterns = await app.state.db.get_token_waste_patterns(days=days, machine_id=machine_id)
    return JSONResponse(patterns)

@app.get("/api/optimization/summary")
async def api_optimization_summary(days: int = 30, machine_id: str = None):
    """Get AI optimization summary dashboard."""
    # Get various optimization metrics
    prompt_analyses = await app.state.db.get_prompt_analysis(limit=10, machine_id=machine_id)
    cache_analytics = await app.state.db.get_cache_analytics(days=days, machine_id=machine_id)
    waste_patterns = await app.state.db.get_token_waste_patterns(days=days, machine_id=machine_id)

    # Calculate summary statistics
    avg_prompt_quality = sum(p.get('quality_score', 0) or 0 for p in prompt_analyses) / max(len(prompt_analyses), 1) if prompt_analyses else 0
    total_waste_tokens = sum(w.get('total_wasted_tokens', 0) or 0 for w in waste_patterns)
    total_cost_savings = sum(c.get('cost_saved_usd', 0) or 0 for c in cache_analytics)
    avg_cache_hit_rate = sum(c.get('cache_hit_rate', 0) or 0 for c in cache_analytics) / max(len(cache_analytics), 1) if cache_analytics else 0

    return JSONResponse({
        "avg_prompt_quality": round(avg_prompt_quality, 2),
        "total_waste_tokens": total_waste_tokens,
        "total_cache_savings": round(total_cost_savings, 2),
        "avg_cache_hit_rate": round(avg_cache_hit_rate, 2),
        "recent_analyses": prompt_analyses[:5],
        "top_waste_patterns": waste_patterns[:5]
    })


# ============================================================================
# CODE ANALYTICS API ENDPOINTS
# ============================================================================

@app.get("/api/analytics/code-modifications")
async def api_get_code_modifications(session_id: str = None, language: str = None, days: int = 30, machine_id: str = None):
    """Get code modifications."""
    modifications = await app.state.db.get_code_modifications(session_id=session_id, language=language, days=days, machine_id=machine_id)
    return JSONResponse(modifications)

@app.post("/api/analytics/code-modifications")
async def api_create_code_modification(data: dict):
    """Log a code modification."""
    timestamp = data.get('timestamp')
    if timestamp and await app.state.db.code_modification_exists(data['session_id'], data['file_path'], timestamp):
        return JSONResponse({"status": "already_exists"})
    mod_id = await app.state.db.create_code_modification(
        session_id=data['session_id'],
        file_path=data['file_path'],
        **{k: v for k, v in data.items() if k not in ['session_id', 'file_path']}
    )
    # Keep the per-user language_stats aggregate in sync with real modifications.
    await app.state.db.recompute_language_stats_for_session(data['session_id'])
    return JSONResponse({"status": "created", "id": mod_id})

@app.get("/api/analytics/language-stats/{user_id}")
async def api_get_language_stats(request: Request, user_id: str):
    """Get language usage statistics for a user."""
    denied = _check_machine_scope(request, user_id)
    if denied:
        return denied
    stats = await app.state.db.get_language_stats(user_id)
    return JSONResponse(stats)

@app.get("/api/analytics/code-quality")
async def api_get_code_quality_metrics(session_id: str = None, language: str = None, days: int = 30, machine_id: str = None):
    """Get code quality metrics."""
    metrics = await app.state.db.get_code_quality_metrics(session_id=session_id, language=language, days=days, machine_id=machine_id)
    return JSONResponse(metrics)

@app.post("/api/analytics/code-quality")
async def api_create_code_quality_metrics(data: dict):
    """Create code quality metrics."""
    metrics_id = await app.state.db.create_code_quality_metrics(
        session_id=data['session_id'],
        **{k: v for k, v in data.items() if k != 'session_id'}
    )
    return JSONResponse({"status": "created", "id": metrics_id})

@app.get("/api/analytics/ai-suggestions")
async def api_get_ai_suggestions(session_id: str = None, suggestion_type: str = None, days: int = 30, machine_id: str = None):
    """Get AI suggestions."""
    suggestions = await app.state.db.get_ai_suggestions(session_id=session_id, suggestion_type=suggestion_type, days=days, machine_id=machine_id)
    return JSONResponse(suggestions)

@app.post("/api/analytics/ai-suggestions")
async def api_create_ai_suggestion(data: dict):
    """Log an AI suggestion."""
    suggestion_id = await app.state.db.create_ai_suggestion(
        session_id=data['session_id'],
        suggestion_type=data['suggestion_type'],
        suggestion_text=data['suggestion_text'],
        **{k: v for k, v in data.items() if k not in ['session_id', 'suggestion_type', 'suggestion_text']}
    )
    return JSONResponse({"status": "created", "id": suggestion_id})

@app.get("/api/analytics/ai-acceptance-rate")
async def api_get_ai_acceptance_rate(days: int = 30, suggestion_type: str = None, machine_id: str = None):
    """Get AI suggestion acceptance rate."""
    rate = await app.state.db.get_ai_suggestion_acceptance_rate(days=days, suggestion_type=suggestion_type, machine_id=machine_id)
    return JSONResponse(rate)

@app.get("/api/analytics/refactoring-impact")
async def api_get_refactoring_impact(days: int = 30, machine_id: str = None):
    """Get real refactoring impact: code modifications where the client's own
    before/after complexity estimate shows a genuine reduction."""
    impact = await app.state.db.get_refactoring_impact(days=days, machine_id=machine_id)
    return JSONResponse(impact)

@app.get("/api/analytics/project-health")
async def api_get_project_health(request: Request, working_directory: str):
    """Get organization-wide project health metrics (admin-only)."""
    denied = _require_admin(request)
    if denied:
        return denied
    health = await app.state.db.get_project_health(working_directory)
    if not health:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    return JSONResponse(health)

@app.get("/api/analytics/file-heatmap")
async def api_file_modification_heatmap(days: int = 30, machine_id: str = None):
    """Get file modification heatmap data."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        query = """SELECT file_path,
                          language,
                          COUNT(*) as modification_count,
                          SUM(lines_added) as total_lines_added,
                          SUM(lines_deleted) as total_lines_deleted,
                          AVG(modification_quality_score) as avg_quality
                   FROM code_modifications
                   WHERE timestamp >= datetime('now', '-' || ? || ' days')"""
        params = [days]
        _frag, _fparams = Database._machine_scope_subquery_clause(machine_id)
        query += _frag
        params.extend(_fparams)
        query += """
                   GROUP BY file_path
                   ORDER BY modification_count DESC
                   LIMIT 50"""
        async with conn.execute(query, params) as cursor:
            data = await cursor.fetchall()
    return JSONResponse(data)

@app.get("/api/analytics/language-breakdown")
async def api_language_breakdown(days: int = 30, machine_id: str = None):
    """Get language usage breakdown."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        query = """SELECT language,
                          COUNT(DISTINCT session_id) as sessions,
                          COUNT(*) as modifications,
                          SUM(lines_added) as total_lines_added,
                          SUM(lines_deleted) as total_lines_deleted,
                          AVG(modification_quality_score) as avg_quality
                   FROM code_modifications
                   WHERE timestamp >= datetime('now', '-' || ? || ' days')
                   AND language IS NOT NULL"""
        params = [days]
        _frag, _fparams = Database._machine_scope_subquery_clause(machine_id)
        query += _frag
        params.extend(_fparams)
        query += """
                   GROUP BY language
                   ORDER BY modifications DESC"""
        async with conn.execute(query, params) as cursor:
            data = await cursor.fetchall()
    return JSONResponse(data)

@app.get("/api/analytics/summary")
async def api_analytics_summary(days: int = 30, machine_id: str = None):
    """Get code analytics summary dashboard."""
    async with app.state.db.get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

        machine_filter, _mf_params = Database._machine_scope_subquery_clause(machine_id)
        mod_params = [days] + _mf_params

        # Total modifications
        async with conn.execute(
            f"SELECT COUNT(*) as count FROM code_modifications WHERE timestamp >= datetime('now', '-' || ? || ' days'){machine_filter}",
            mod_params
        ) as cursor:
            total_mods = (await cursor.fetchone())['count']

        # Language distribution
        async with conn.execute(
            f"""SELECT language, COUNT(*) as count
               FROM code_modifications
               WHERE timestamp >= datetime('now', '-' || ? || ' days')
               AND language IS NOT NULL{machine_filter}
               GROUP BY language
               ORDER BY count DESC
               LIMIT 5""",
            mod_params
        ) as cursor:
            lang_dist = await cursor.fetchall()

        # AI acceptance rate
        acceptance_rate = await app.state.db.get_ai_suggestion_acceptance_rate(days=days, machine_id=machine_id)

        # Average code quality
        quality_filter, _qf_params = Database._machine_scope_subquery_clause(machine_id)
        quality_params = [days] + _qf_params
        async with conn.execute(
            f"SELECT AVG(overall_quality_score) as avg_quality FROM code_quality_metrics WHERE analyzed_at >= datetime('now', '-' || ? || ' days'){quality_filter}",
            quality_params
        ) as cursor:
            row = await cursor.fetchone()
            avg_quality = row['avg_quality'] if row and row['avg_quality'] else 0

    return JSONResponse({
        "total_modifications": total_mods,
        "language_distribution": lang_dist,
        "ai_acceptance_rate": acceptance_rate.get('acceptance_rate', 0),
        "avg_code_quality": round(avg_quality, 2)
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=Config.HOST,
        port=Config.PORT,
        log_level="info"
    )
