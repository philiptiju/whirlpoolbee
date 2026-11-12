import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def agent_client(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, agent_token, first_seen, last_seen)
               VALUES ('victim', 'victim', 'SECRET', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, working_directory)
               VALUES ('victim-session', 'victim', CURRENT_TIMESTAMP, '/victim')"""
        )
    with TestClient(api_app) as client:
        yield client


def _register(client, machine_id):
    response = client.post("/api/client/register", json={"machine_id": machine_id, "machine_name": machine_id})
    assert response.status_code == 200
    return {"X-Agent-Token": response.json()["agent_token"]}


@pytest.mark.parametrize("path,payload", [
    ("/api/client/heartbeat", {"machine_id": "ghost"}),
    ("/api/client/sync-log", {"machine_id": "ghost", "source": "jsonl"}),
    ("/api/client/session", {"machine_id": "ghost", "session_id": "s", "start_time": "2026-01-01",
                             "working_directory": "/"}),
    ("/api/client/message", {"machine_id": "ghost", "session_id": "victim-session",
                             "timestamp": "2026-01-01", "role": "assistant", "content": "x"}),
    ("/api/client/token_usage", {"machine_id": "ghost", "session_id": "victim-session",
                                 "timestamp": "2026-01-01", "total_tokens": 1}),
])
def test_unregistered_machine_cannot_write(agent_client, database, path, payload):
    assert agent_client.post(path, json=payload).status_code == 403
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE machine_id = 'ghost'").fetchone()[0] == 0


def test_registered_machine_cannot_write_into_foreign_session(agent_client, database):
    headers = _register(agent_client, "attacker")
    base = {"machine_id": "attacker", "session_id": "victim-session", "timestamp": "2026-01-01"}
    assert agent_client.post("/api/client/message", headers=headers,
                             json={**base, "role": "assistant", "content": "INJECTED"}).status_code == 403
    assert agent_client.post("/api/client/token_usage", headers=headers,
                             json={**base, "total_tokens": 10**9}).status_code == 403
    assert agent_client.post("/api/client/session", headers=headers,
                             json={**base, "start_time": "2026-01-01", "working_directory": "/"}).status_code == 403
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 0
        assert conn.execute(
            "SELECT machine_id FROM sessions WHERE session_id = 'victim-session'"
        ).fetchall() == [("victim",)]


def test_registered_machine_syncs_own_session(agent_client, database):
    headers = _register(agent_client, "laptop")
    base = {"machine_id": "laptop", "session_id": "own", "timestamp": "2026-01-01"}
    assert agent_client.post("/api/client/session", headers=headers,
                             json={**base, "start_time": "2026-01-01", "working_directory": "/"}).json()["status"] == "created"
    assert agent_client.post("/api/client/message", headers=headers,
                             json={**base, "role": "user", "content": "hi"}).json()["status"] == "logged"
    assert agent_client.post("/api/client/token_usage", headers=headers,
                             json={**base, "total_tokens": 5}).json()["status"] == "logged"
    assert agent_client.post("/api/client/heartbeat", headers=headers,
                             json={"machine_id": "laptop"}).status_code == 200
    assert agent_client.post("/api/client/message",
                             json={**base, "role": "user", "content": "no token"}).status_code == 403


def test_legacy_machine_receives_token_on_http_register(agent_client, database):
    """Regression test: a machine that already has a `machines` row but no
    agent_token yet (pre-upgrade legacy machine, e.g. every machine in the
    fleet before the agent-token feature shipped) must receive its freshly
    issued token in the /api/client/register HTTP response so it can save
    it locally. A prior bug called verify_or_issue_agent_token() a second,
    redundant time after the token was already issued+persisted by the
    first call, causing the second call's own match-check to fail (the
    caller's `provided` token is still absent) and silently discarding the
    issued token -- the machine got HTTP 200 but no token, then was
    permanently rejected (403) on every subsequent request, including its
    own next registration attempt."""
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, first_seen, last_seen)
               VALUES ('legacy-laptop', 'legacy-laptop', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )

    response = agent_client.post(
        "/api/client/register",
        json={"machine_id": "legacy-laptop", "machine_name": "legacy-laptop"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("agent_token"), "legacy machine must receive a token on first post-upgrade register"

    # The returned token must actually be usable on the very next request.
    headers = {"X-Agent-Token": body["agent_token"]}
    assert agent_client.post(
        "/api/client/heartbeat", headers=headers, json={"machine_id": "legacy-laptop"}
    ).status_code == 200

    # And the machine must be able to register again later (e.g. on
    # restart) using that same saved token, without being locked out.
    response2 = agent_client.post(
        "/api/client/register",
        headers=headers,
        json={"machine_id": "legacy-laptop", "machine_name": "legacy-laptop"},
    )
    assert response2.status_code == 200


def test_legacy_token_issued_once_under_concurrency(database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, first_seen, last_seen)
               VALUES ('legacy', 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )

    async def race():
        return await asyncio.gather(
            *(database.verify_or_issue_agent_token("legacy", None) for _ in range(10))
        )

    results = asyncio.run(race())
    issued = [token for allowed, token in results if allowed and token]
    assert len(issued) == 1
    assert sum(1 for allowed, _ in results if allowed) == 1
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT agent_token FROM machines WHERE machine_id = 'legacy'"
        ).fetchone()[0] == issued[0]
