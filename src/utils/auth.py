"""Multi-provider login for WhirlpoolBee.

Every provider (Microsoft/Azure AD, Google, GitHub, or any other generic
OIDC/OAuth2 provider) is configured entirely through the Admin -> Login
Providers UI and stored in the auth_providers table (schema_v4.sql) --
there are no required environment variables, and adding/changing/removing a
provider never needs a redeploy. See src/database.py's LOGIN PROVIDERS
section for the DB layer and src/utils/crypto.py for how client secrets are
encrypted at rest.

Azure AD uses the MSAL authorization-code flow (robust id_token signature
validation). Every other provider_type (google/github/generic_oauth2) uses a
small hand-rolled OAuth2 authorization-code flow via httpx -- exchange the
code for an access token, then call the provider's userinfo endpoint. Admin-
created local (username/password) accounts are handled separately in
src/utils/passwords.py / the /auth/login/local route in main.py; they don't
go through any of the flows here.

The signed-in user's claims (name/email) are cached in the (signed, server-
side-secret) session cookie -- no provider tokens are persisted anywhere.
"""

import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx
import msal
from starlette.requests import Request

# Paths that must remain reachable without an authenticated session:
# the auth flow itself, static assets, container health probes, and the
# machine-agent ingestion endpoints (client_agent.py runs unattended on
# users' machines and can't complete an interactive browser login).
PUBLIC_PATH_PREFIXES = (
    "/auth/",
    "/static/",
    "/api/health",
    "/api/client/",
    "/api/agent/version",
)

# Well-known endpoints for provider_types that don't require the admin to
# type them in manually -- just a Client ID + Client Secret (and, for Azure
# AD, a Tenant ID). 'generic_oauth2' has no preset: the admin supplies every
# URL, for any OIDC/OAuth2-compatible provider not listed here (Okta, Auth0,
# a self-hosted Keycloak, etc).
PROVIDER_PRESETS = {
    "google": {
        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scopes": "openid email profile",
    },
    "github": {
        "authorization_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scopes": "read:user user:email",
    },
}


def is_public_path(path: str, root_path: str = "") -> bool:
    """Return True if `path` should be reachable without authentication."""
    if root_path and path.startswith(root_path):
        path = path[len(root_path):] or "/"
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def _external_base_url(request: Request) -> str:
    """Base URL as seen by the client, honoring X-Forwarded-Proto/Host set by
    a reverse proxy in front of this app (e.g. TLS terminating upstream and
    forwarding plain HTTP) -- request.base_url alone would report "http"
    even though the browser is on "https", breaking the redirect URI match
    every provider enforces."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{scheme}://{host}"


def _root_path(request: Request) -> str:
    return getattr(request.state, "root_path", "") or ""


def get_redirect_uri(request: Request, provider_key: Optional[str] = None) -> str:
    """Build the callback redirect URI for a given provider.

    Azure AD keeps the legacy, suffix-less `/auth/callback` path (so
    existing Azure App registrations with that redirect URI already
    configured keep working unchanged); every other provider uses
    `/auth/callback/<provider_key>` so multiple non-Azure providers can
    coexist without colliding."""
    suffix = "/auth/callback" if provider_key in (None, "azure-ad") else f"/auth/callback/{provider_key}"
    return f"{_external_base_url(request)}{_root_path(request)}{suffix}"


# ============================================================================
# AZURE AD (via MSAL)
# ============================================================================

def _msal_app(provider: dict) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        provider["client_id"],
        authority=f"https://login.microsoftonline.com/{provider['tenant_id']}",
        client_credential=provider["client_secret"],
    )


def build_azure_auth_code_url(request: Request, provider: dict) -> str:
    """Start the Azure AD login flow: stash MSAL's flow state in the
    session and return the Microsoft sign-in URL to redirect to."""
    flow = _msal_app(provider).initiate_auth_code_flow(
        scopes=["User.Read"],
        redirect_uri=get_redirect_uri(request, "azure-ad"),
        state=str(uuid.uuid4()),
        # form_post delivers the (often 4-5k+ char) authorization code as a
        # POST body instead of appending it to the redirect_uri's query
        # string -- the query-string version gets silently blocked (403, no
        # trace in proxy logs) by upstream WAFs/proxies that cap URL length
        # before the request reaches the app.
        response_mode="form_post",
    )
    request.session["auth_flow"] = flow
    request.session["auth_provider_key"] = provider["provider_key"]
    return flow["auth_uri"]


def acquire_azure_token_by_auth_code(request: Request, provider: dict, auth_response: dict) -> dict:
    """Complete the Azure AD login flow with the callback params (form body,
    since response_mode=form_post above). Returns the MSAL token/claims
    result (has an "error" key on failure)."""
    flow = request.session.pop("auth_flow", {})
    return _msal_app(provider).acquire_token_by_auth_code_flow(flow, auth_response)


def azure_logout_url(request: Request, provider: dict) -> str:
    """Microsoft's end-session endpoint, redirecting back to the app root
    after the user is signed out of their Microsoft session too."""
    post_logout = f"{_external_base_url(request)}{_root_path(request)}/"
    return (
        f"https://login.microsoftonline.com/{provider['tenant_id']}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={post_logout}"
    )


# ============================================================================
# GENERIC OAUTH2 (google / github / generic_oauth2 provider types)
# ============================================================================

def build_generic_auth_code_url(request: Request, provider: dict) -> str:
    """Start a generic OAuth2 authorization-code flow: stash a CSRF state
    token in the session and return the provider's consent-screen URL."""
    state = str(uuid.uuid4())
    request.session["oauth_state"] = state
    request.session["auth_provider_key"] = provider["provider_key"]
    params = {
        "client_id": provider["client_id"],
        "redirect_uri": get_redirect_uri(request, provider["provider_key"]),
        "response_type": "code",
        "scope": provider.get("scopes") or PROVIDER_PRESETS.get(provider["provider_type"], {}).get("scopes", ""),
        "state": state,
    }
    auth_url = provider.get("authorization_url") or PROVIDER_PRESETS[provider["provider_type"]]["authorization_url"]
    return f"{auth_url}?{urlencode(params)}"


async def complete_generic_oauth2_login(request: Request, provider: dict, params: dict) -> dict:
    """Validate state, exchange the authorization code for an access token,
    fetch the provider's userinfo endpoint, and return the normalized
    {name, email, sub} dict -- or {"error": "..."} on any failure."""
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or params.get("state") != expected_state:
        return {"error": "State mismatch -- please try signing in again."}
    code = params.get("code")
    if not code:
        return {"error": params.get("error_description") or params.get("error") or "No authorization code returned."}

    token_url = provider.get("token_url") or PROVIDER_PRESETS[provider["provider_type"]]["token_url"]
    userinfo_url = provider.get("userinfo_url") or PROVIDER_PRESETS[provider["provider_type"]]["userinfo_url"]
    redirect_uri = get_redirect_uri(request, provider["provider_key"])

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            token_url,
            data={
                "client_id": provider["client_id"],
                "client_secret": provider["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code >= 400:
            return {"error": f"Token exchange failed ({token_resp.status_code})."}
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return {"error": token_data.get("error_description") or "No access token returned."}

        userinfo_resp = await client.get(
            userinfo_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json" if provider["provider_type"] == "github" else "application/json",
            },
        )
        if userinfo_resp.status_code >= 400:
            return {"error": f"Could not fetch profile info ({userinfo_resp.status_code})."}
        userinfo = userinfo_resp.json()

        email = userinfo.get("email")
        if provider["provider_type"] == "github" and not email:
            # GitHub omits email from /user when the user's email is private;
            # fall back to their primary verified address from /user/emails
            # (requires the user:email scope, included in our default scopes).
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            if emails_resp.status_code < 400:
                for entry in emails_resp.json():
                    if entry.get("primary") and entry.get("verified"):
                        email = entry.get("email")
                        break

    if not email:
        return {"error": "Your account's email address isn't available/verified with this provider."}

    name = userinfo.get("name") or userinfo.get("login") or userinfo.get("preferred_username") or email
    sub = str(userinfo.get("sub") or userinfo.get("id") or userinfo.get("login") or email)
    return {"name": name, "email": email, "oid": sub}


# ============================================================================
# SESSION HELPERS (provider-agnostic)
# ============================================================================

def user_from_claims(claims: dict) -> dict:
    """Extract the small set of user fields we display/store in the session,
    from an Azure AD id_token's claims."""
    return {
        "name": claims.get("name") or claims.get("preferred_username") or "User",
        "email": claims.get("preferred_username") or claims.get("email") or "",
        "oid": claims.get("oid") or claims.get("sub"),
    }


def current_user(request: Request) -> Optional[dict]:
    """Return the signed-in user dict stored in the session, or None."""
    return request.session.get("user")
