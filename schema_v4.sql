-- Schema v4: Multi-provider login (database-driven SSO/OAuth2 providers +
-- admin-created local username/password accounts).
-- Claude Local Dashboard (WhirlpoolBee) — additive only, loaded after
-- schema_v3.sql. All statements use IF NOT EXISTS so this file is safe to
-- re-run against an existing database.

-- ============================================================================
-- SSO / OAUTH2 LOGIN PROVIDERS
--
-- Fully UI-configurable (Admin -> Login Providers): an admin can add, edit,
-- enable/disable, and delete any number of providers without touching
-- environment variables or redeploying. provider_type drives which flow is
-- used and which fields are required:
--   - 'azure_ad'      : client_id, client_secret, tenant_id (MSAL flow)
--   - 'google'        : client_id, client_secret (endpoints are well-known)
--   - 'github'        : client_id, client_secret (endpoints are well-known)
--   - 'generic_oauth2': client_id, client_secret, authorization_url,
--                        token_url, userinfo_url, scopes (admin supplies
--                        every endpoint -- for Okta/Auth0/any other OIDC
--                        provider not covered by a built-in preset)
-- client_secret is stored encrypted (see src/utils/crypto.py) using a key
-- derived from DASHBOARD_SESSION_SECRET -- never in plaintext.
-- ============================================================================

CREATE TABLE IF NOT EXISTS auth_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT UNIQUE NOT NULL,       -- URL-safe slug, e.g. 'google', 'github', 'azure-ad', or a custom slug for generic providers
    provider_type TEXT NOT NULL,             -- 'azure_ad' | 'google' | 'github' | 'generic_oauth2'
    display_name TEXT NOT NULL,              -- shown on the login button, e.g. "Sign in with Google"
    client_id TEXT NOT NULL,
    client_secret_encrypted TEXT NOT NULL,
    authorization_url TEXT,                  -- generic_oauth2 only (google/github/azure_ad compute this)
    token_url TEXT,                          -- generic_oauth2 only
    userinfo_url TEXT,                       -- generic_oauth2 only
    scopes TEXT,                             -- space-separated; generic_oauth2 only (others use a built-in default)
    tenant_id TEXT,                          -- azure_ad only
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- LOCAL (USERNAME/PASSWORD) ACCOUNTS
--
-- Admin-created only (Admin -> Login Providers -> Local Accounts) -- there
-- is intentionally no public self-service sign-up page. A local account's
-- email is the same identity key used everywhere else (admin_users,
-- super_users, machines.logged_in_user matching), so once created it
-- participates in every existing permission check with no further wiring.
-- ============================================================================

CREATE TABLE IF NOT EXISTS local_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    password_hash TEXT NOT NULL,              -- pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex> -- see src/utils/passwords.py
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);
