import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    with TestClient(api_app) as client:
        yield client


def _seed_machine(conn, machine_id, logged_in_user, with_session=True):
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO machines
           (machine_id, machine_name, os_type, logged_in_user, agent_version,
            first_seen, last_seen, is_active)
           VALUES (?, ?, 'darwin', ?, '1.0.0', ?, ?, 1)""",
        (machine_id, f"{machine_id}-host", logged_in_user, now, now),
    )
    session_id = f"{machine_id}-session-1"
    if with_session:
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time)
               VALUES (?, ?, ?)""",
            (session_id, machine_id, now),
        )
        conn.execute(
            """INSERT INTO token_usage
               (timestamp, session_id, machine_id, model_name, input_tokens,
                output_tokens, total_tokens, estimated_cost_usd)
               VALUES (?, ?, ?, 'claude', 10, 10, 20, 0.5)""",
            (now, session_id, machine_id),
        )
        conn.execute(
            """INSERT INTO messages (session_id, machine_id, timestamp, role, content)
               VALUES (?, ?, ?, 'user', 'hello')""",
            (session_id, machine_id, now),
        )
        conn.execute(
            "INSERT INTO session_tags (session_id, tag_id, source) VALUES (?, 1, 'manual')",
            (session_id,),
        )
    conn.execute(
        """INSERT INTO sync_log (machine_id, source, items_total, items_synced, items_failed)
           VALUES (?, 'sqlite', 1, 1, 0)""",
        (machine_id,),
    )
    # user_skills/language_stats are one row per (user_id, category/language)
    # regardless of how many machines that user has, so use INSERT OR IGNORE
    # when seeding a second machine for the same logged_in_user.
    conn.execute(
        """INSERT OR IGNORE INTO user_skills (user_id, skill_category, skill_level, last_activity)
           VALUES (?, 'code_quality', 50, ?)""",
        (logged_in_user, now),
    )
    conn.execute(
        """INSERT OR IGNORE INTO language_stats (user_id, language, files_modified)
           VALUES (?, 'python', 5)""",
        (logged_in_user,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO anomalies
           (detected_at, scope_type, scope_id, metric, metric_date, observed_value, severity)
           VALUES (?, 'machine', ?, 'daily_cost', date('now'), 100, 'high')""",
        (now, machine_id),
    )
    conn.execute(
        """INSERT OR IGNORE INTO anomalies
           (detected_at, scope_type, scope_id, metric, metric_date, observed_value, severity)
           VALUES (?, 'user', ?, 'daily_tokens', date('now'), 200, 'medium')""",
        (now, logged_in_user),
    )
    conn.commit()
    return session_id


@pytest.mark.asyncio
async def test_delete_machine_completely_removes_all_rows(database):
    with sqlite3.connect(database.db_path) as conn:
        session_id = _seed_machine(conn, "machine-a", "alice")

    result = await database.delete_machine_completely("machine-a")
    assert result["machine_id"] == "machine-a"
    assert result["logged_in_user"] == "alice"
    assert result["sessions_deleted"] == 1
    assert result["user_data_purged"] is True

    with sqlite3.connect(database.db_path) as conn:
        def count(table, where, params):
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]

        assert count("machines", "machine_id = ?", ("machine-a",)) == 0
        assert count("sessions", "machine_id = ?", ("machine-a",)) == 0
        assert count("token_usage", "session_id = ?", (session_id,)) == 0
        assert count("messages", "session_id = ?", (session_id,)) == 0
        assert count("session_tags", "session_id = ?", (session_id,)) == 0
        assert count("sync_log", "machine_id = ?", ("machine-a",)) == 0
        assert count("user_skills", "user_id = ?", ("alice",)) == 0
        assert count("language_stats", "user_id = ?", ("alice",)) == 0
        assert count("anomalies", "scope_id IN (?, ?)", ("machine-a", "alice")) == 0


@pytest.mark.asyncio
async def test_delete_machine_completely_unknown_machine_returns_none(database):
    assert await database.delete_machine_completely("does-not-exist") is None


@pytest.mark.asyncio
async def test_delete_machine_completely_preserves_other_machines_and_users(database):
    with sqlite3.connect(database.db_path) as conn:
        _seed_machine(conn, "machine-a", "alice")
        _seed_machine(conn, "machine-b", "bob")

    await database.delete_machine_completely("machine-a")

    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM machines WHERE machine_id = 'machine-b'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM user_skills WHERE user_id = 'bob'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_delete_machine_completely_keeps_shared_user_data_for_other_machine(database):
    """If the same logged_in_user has a second machine, deleting one must
    NOT purge the user_id-keyed gamification tables -- that data still
    legitimately belongs to the remaining machine."""
    with sqlite3.connect(database.db_path) as conn:
        _seed_machine(conn, "machine-a", "shared-user")
        _seed_machine(conn, "machine-b", "shared-user")

    result = await database.delete_machine_completely("machine-a")
    assert result["user_data_purged"] is False

    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM user_skills WHERE user_id = 'shared-user'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM machines WHERE machine_id = 'machine-a'"
        ).fetchone()[0] == 0


def test_delete_machine_endpoint_requires_admin(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        _seed_machine(conn, "machine-a", "alice")
    with TestClient(api_app) as client:
        assert client.delete("/api/machines/machine-a").status_code == 403


def test_delete_machine_endpoint_deletes_and_returns_summary(admin_client, database):
    with sqlite3.connect(database.db_path) as conn:
        _seed_machine(conn, "machine-a", "alice")

    response = admin_client.delete("/api/machines/machine-a")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted"
    assert body["machine_id"] == "machine-a"
    assert body["sessions_deleted"] == 1

    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM machines WHERE machine_id = 'machine-a'"
        ).fetchone()[0] == 0


def test_delete_machine_endpoint_404_for_unknown_machine(admin_client):
    response = admin_client.delete("/api/machines/does-not-exist")
    assert response.status_code == 404
