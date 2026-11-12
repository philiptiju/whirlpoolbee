"""Regression tests for the DB-driven, multi-provider login system (see
schema_v4.sql, Database's "LOGIN PROVIDERS" section, src/utils/auth.py,
src/utils/crypto.py, src/utils/passwords.py, and the /auth/* +
/api/admin/auth-providers + /api/admin/local-users routes in
src/api/main.py)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.utils.crypto import encrypt_secret, decrypt_secret
from src.utils.passwords import hash_password, verify_password


@pytest.fixture(autouse=True)
def _no_azure_ad_env_migration(monkeypatch):
    """Neutralize this dev box's real .env Azure AD credentials for every
    test in this file (Database.initialize already ran the one-time env
    migration against them via the `database` fixture's real .env, which
    would otherwise make `is_auth_required()`/`list_auth_providers()`
    unpredictably non-empty). Tests that specifically exercise the
    migration set their own values afterwards, which take precedence."""
    from src.utils.config import Config

    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_ID", "")
    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_SECRET", "")
    monkeypatch.setattr(Config, "AZURE_AD_TENANT_ID", "")


# ============================================================================
# CRYPTO / PASSWORD HELPERS
# ============================================================================

def test_encrypt_decrypt_secret_round_trips():
    ciphertext = encrypt_secret("super-secret-value")
    assert ciphertext != "super-secret-value"
    assert decrypt_secret(ciphertext) == "super-secret-value"


def test_hash_password_verifies_correct_and_rejects_incorrect():
    hashed = hash_password("CorrectHorse1")
    assert hashed != "CorrectHorse1"
    assert verify_password("CorrectHorse1", hashed) is True
    assert verify_password("WrongPassword", hashed) is False


def test_hash_password_produces_different_hashes_for_same_password():
    # Random per-call salt means two hashes of the same password must
    # differ, even though both still verify correctly.
    first = hash_password("SamePassword1")
    second = hash_password("SamePassword1")
    assert first != second
    assert verify_password("SamePassword1", first)
    assert verify_password("SamePassword1", second)


# ============================================================================
# DATABASE: auth_providers / local_users CRUD + is_auth_required
# ============================================================================

@pytest.mark.asyncio
async def test_is_auth_required_false_until_a_provider_or_local_user_exists(database):
    assert await database.is_auth_required() is False

    await database.create_auth_provider(
        provider_key="google",
        provider_type="google",
        display_name="Google",
        client_id="client-id",
        client_secret_encrypted=encrypt_secret("secret"),
    )
    assert await database.is_auth_required() is True


@pytest.mark.asyncio
async def test_is_auth_required_true_once_a_local_user_exists(database):
    assert await database.is_auth_required() is False
    await database.create_local_user("user@example.com", "Some User", hash_password("Password1"))
    assert await database.is_auth_required() is True


@pytest.mark.asyncio
async def test_is_auth_required_ignores_disabled_providers(database):
    provider_id = await database.create_auth_provider(
        provider_key="github",
        provider_type="github",
        display_name="GitHub",
        client_id="client-id",
        client_secret_encrypted=encrypt_secret("secret"),
        enabled=False,
    )
    assert await database.is_auth_required() is False
    await database.update_auth_provider(provider_id, enabled=True)
    assert await database.is_auth_required() is True


@pytest.mark.asyncio
async def test_auth_provider_crud_round_trip_masks_secret_by_default(database):
    provider_id = await database.create_auth_provider(
        provider_key="azure-ad",
        provider_type="azure_ad",
        display_name="Microsoft",
        client_id="client-id",
        client_secret_encrypted=encrypt_secret("top-secret"),
        tenant_id="tenant-guid",
    )

    listed = await database.list_auth_providers()
    assert len(listed) == 1
    assert listed[0]["has_secret"] is True
    assert "client_secret_encrypted" not in listed[0]

    fetched = await database.get_auth_provider("azure-ad", include_secret=True)
    assert decrypt_secret(fetched["client_secret_encrypted"]) == "top-secret"

    updated = await database.update_auth_provider(provider_id, display_name="Microsoft Entra ID")
    assert updated is True
    fetched_again = await database.get_auth_provider("azure-ad")
    assert fetched_again["display_name"] == "Microsoft Entra ID"

    removed = await database.delete_auth_provider(provider_id)
    assert removed is True
    assert await database.get_auth_provider("azure-ad") is None


@pytest.mark.asyncio
async def test_create_auth_provider_rejects_duplicate_provider_key(database):
    kwargs = dict(
        provider_key="google",
        provider_type="google",
        display_name="Google",
        client_id="client-id",
        client_secret_encrypted=encrypt_secret("secret"),
    )
    await database.create_auth_provider(**kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        await database.create_auth_provider(**kwargs)


@pytest.mark.asyncio
async def test_local_user_crud_round_trip_and_login_verification(database):
    user_id = await database.create_local_user(
        "Person@Example.com", "A Person", hash_password("GoodPassword1")
    )

    # Lookup is case-insensitive on email (stored lowercased).
    fetched = await database.get_local_user_by_email("person@example.com")
    assert fetched is not None
    assert verify_password("GoodPassword1", fetched["password_hash"])
    assert verify_password("WrongPassword", fetched["password_hash"]) is False

    listed = await database.list_local_users()
    assert len(listed) == 1
    assert "password_hash" not in listed[0]

    await database.touch_local_user_login(user_id)
    refreshed = await database.get_local_user_by_email("person@example.com")
    assert refreshed["last_login_at"] is not None

    updated = await database.update_local_user(user_id, enabled=False)
    assert updated is True
    disabled = await database.get_local_user_by_email("person@example.com")
    assert disabled["enabled"] == 0

    removed = await database.delete_local_user(user_id)
    assert removed is True
    assert await database.get_local_user_by_email("person@example.com") is None


@pytest.mark.asyncio
async def test_create_local_user_rejects_duplicate_email(database):
    await database.create_local_user("dup@example.com", "First", hash_password("Password1"))
    with pytest.raises(sqlite3.IntegrityError):
        await database.create_local_user("dup@example.com", "Second", hash_password("Password2"))


# ============================================================================
# ONE-TIME AZURE AD ENV-VAR MIGRATION
# ============================================================================

@pytest.mark.asyncio
async def test_azure_ad_env_migration_creates_provider_once(database, monkeypatch):
    from src.utils.config import Config

    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_ID", "env-client-id")
    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_SECRET", "env-client-secret")
    monkeypatch.setattr(Config, "AZURE_AD_TENANT_ID", "env-tenant-id")

    with sqlite3.connect(database.db_path) as conn:
        database._migrate_azure_ad_env_to_auth_provider(conn)

    provider = await database.get_auth_provider("azure-ad", include_secret=True)
    assert provider is not None
    assert provider["client_id"] == "env-client-id"
    assert decrypt_secret(provider["client_secret_encrypted"]) == "env-client-secret"
    assert provider["tenant_id"] == "env-tenant-id"

    # Re-running must not duplicate/overwrite an already-migrated provider.
    with sqlite3.connect(database.db_path) as conn:
        database._migrate_azure_ad_env_to_auth_provider(conn)
    assert len(await database.list_auth_providers()) == 1


@pytest.mark.asyncio
async def test_azure_ad_env_migration_is_noop_when_vars_are_missing(database, monkeypatch):
    from src.utils.config import Config

    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_ID", "")
    monkeypatch.setattr(Config, "AZURE_AD_CLIENT_SECRET", "")
    monkeypatch.setattr(Config, "AZURE_AD_TENANT_ID", "")

    with sqlite3.connect(database.db_path) as conn:
        database._migrate_azure_ad_env_to_auth_provider(conn)

    assert await database.list_auth_providers() == []


# ============================================================================
# ADMIN API ROUTES (/api/admin/auth-providers, /api/admin/local-users)
# ============================================================================

@pytest.fixture
def admin_client(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    with TestClient(api_app) as client:
        yield client


def test_auth_provider_admin_routes_require_admin(api_app):
    with TestClient(api_app) as client:
        assert client.get("/api/admin/auth-providers").status_code == 403
        assert client.post("/api/admin/auth-providers", json={}).status_code == 403
        assert client.get("/api/admin/local-users").status_code == 403


def test_auth_provider_admin_crud_via_api(admin_client, database):
    create_resp = admin_client.post("/api/admin/auth-providers", json={
        "provider_key": "google",
        "provider_type": "google",
        "display_name": "Google",
        "client_id": "client-id",
        "client_secret": "super-secret",
    })
    assert create_resp.status_code == 200
    provider_id = create_resp.json()["id"]

    list_resp = admin_client.get("/api/admin/auth-providers")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert len(body["providers"]) == 1
    assert body["providers"][0]["has_secret"] is True
    assert "client_secret" not in body["providers"][0]
    assert "google" in body["presets"]

    update_resp = admin_client.put(f"/api/admin/auth-providers/{provider_id}", json={
        "display_name": "Google Workspace",
    })
    assert update_resp.status_code == 200
    assert admin_client.get("/api/admin/auth-providers").json()["providers"][0]["display_name"] == "Google Workspace"

    delete_resp = admin_client.delete(f"/api/admin/auth-providers/{provider_id}")
    assert delete_resp.status_code == 200
    assert admin_client.get("/api/admin/auth-providers").json()["providers"] == []


def test_auth_provider_admin_create_rejects_duplicate_key(admin_client):
    payload = {
        "provider_key": "github",
        "provider_type": "github",
        "display_name": "GitHub",
        "client_id": "client-id",
        "client_secret": "secret",
    }
    assert admin_client.post("/api/admin/auth-providers", json=payload).status_code == 200
    duplicate = admin_client.post("/api/admin/auth-providers", json=payload)
    assert duplicate.status_code == 409


def test_local_user_admin_crud_via_api(admin_client):
    create_resp = admin_client.post("/api/admin/local-users", json={
        "email": "newuser@example.com",
        "name": "New User",
        "password": "LongEnough1",
    })
    assert create_resp.status_code == 200
    user_id = create_resp.json()["id"]

    list_resp = admin_client.get("/api/admin/local-users")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
    assert "password_hash" not in list_resp.json()[0]

    short_password_resp = admin_client.post("/api/admin/local-users", json={
        "email": "another@example.com", "name": "", "password": "short",
    })
    assert short_password_resp.status_code == 400

    update_resp = admin_client.put(f"/api/admin/local-users/{user_id}", json={"enabled": False})
    assert update_resp.status_code == 200
    list_resp_after = admin_client.get("/api/admin/local-users").json()
    assert list_resp_after[0]["enabled"] == 0

    delete_resp = admin_client.delete(f"/api/admin/local-users/{user_id}")
    assert delete_resp.status_code == 200
    assert admin_client.get("/api/admin/local-users").json() == []


# ============================================================================
# LOGIN FLOW (session-cookie based -- needs a client that does NOT
# monkeypatch auth_utils.current_user, unlike the shared api_app fixture, so
# the real session cookie set by /auth/login/local is actually exercised).
# ============================================================================

@pytest.fixture
def login_flow_client(database, monkeypatch, tmp_path):
    from src.utils.config import Config

    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(Config, "DB_PATH", database.db_path)
    monkeypatch.setattr(Config, "AUTH_OVERRIDE", True)
    from src.api import main

    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main.app.state, "db", database)
    monkeypatch.setattr(main, "ROOT_PATH", "")

    # https base_url: SessionMiddleware sets the cookie with https_only=True,
    # which a plain http TestClient request would silently drop.
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as client:
        yield client


def test_local_login_rejects_wrong_password(login_flow_client, database):
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        database.create_local_user("user@example.com", "A User", hash_password("CorrectPassword1"))
    )
    response = login_flow_client.post(
        "/auth/login/local", data={"email": "user@example.com", "password": "WrongPassword"}
    )
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
    # No session cookie should grant access to a protected page.
    assert login_flow_client.get("/admin/login-providers").status_code == 307


def test_local_login_succeeds_and_grants_session(login_flow_client, database):
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        database.create_local_user("user@example.com", "A User", hash_password("CorrectPassword1"))
    )
    response = login_flow_client.post(
        "/auth/login/local", data={"email": "user@example.com", "password": "CorrectPassword1"}
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"

    # Session cookie now lets a non-admin user through (open-access admin
    # fallback doesn't apply once auth_enabled is true and they're signed
    # in but not an admin -- they just don't get is_admin=True).
    home = login_flow_client.get("/")
    assert home.status_code == 200


def test_logout_clears_session(login_flow_client, database):
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        database.create_local_user("user@example.com", "A User", hash_password("CorrectPassword1"))
    )
    login_flow_client.post(
        "/auth/login/local", data={"email": "user@example.com", "password": "CorrectPassword1"}
    )
    assert login_flow_client.get("/").status_code == 200

    logout_resp = login_flow_client.get("/auth/logout")
    assert logout_resp.status_code == 307
    assert login_flow_client.get("/").status_code == 307


def test_login_page_lists_enabled_providers_and_local_form(login_flow_client, database):
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        database.create_auth_provider(
            provider_key="google",
            provider_type="google",
            display_name="Google",
            client_id="client-id",
            client_secret_encrypted=encrypt_secret("secret"),
        )
    )
    asyncio.get_event_loop().run_until_complete(
        database.create_local_user("user@example.com", "A User", hash_password("CorrectPassword1"))
    )
    response = login_flow_client.get("/auth/login")
    assert response.status_code == 200
    assert "Sign in with Google" in response.text
    assert "/auth/login/local" in response.text
