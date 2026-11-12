import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def scoped_client(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        for machine, email, tokens in [
            ("mine", "owner@example.com", 10),
            ("other", "other@example.com", 900),
        ]:
            conn.execute(
                """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (machine, machine, email),
            )
            conn.execute(
                """INSERT INTO sessions (session_id, machine_id, start_time, working_directory, duration_seconds)
                   VALUES (?, ?, CURRENT_TIMESTAMP, ?, 3600)""",
                (machine, machine, "/" + machine),
            )
            conn.execute(
                """INSERT INTO token_usage (session_id, machine_id, timestamp, total_tokens, estimated_cost_usd)
                   VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)""",
                (machine, machine, tokens, tokens / 10),
            )
            conn.execute(
                "INSERT INTO launches (machine_id, timestamp) VALUES (?, CURRENT_TIMESTAMP)",
                (machine,),
            )
        conn.execute("INSERT INTO groups (name) VALUES ('Engineering')")
        conn.execute(
            "INSERT INTO project_health (working_directory, overall_health_score) VALUES ('/other', 90)"
        )
    with TestClient(api_app) as client:
        yield client


@pytest.mark.parametrize("requested", ["", "?machine_id=other", "?machine_id=all"])
def test_analytics_ignore_unauthorized_query_scope(scoped_client, requested):
    response = scoped_client.get("/api/stats/overview" + requested)
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_sessions"] == stats["active_sessions"] == stats["total_launches"] == 1
    assert stats["total_tokens"] == 10
    assert stats["total_cost"] == 1
    projects = scoped_client.get("/api/stats/repositories" + requested).json()
    assert [p["working_directory"] for p in projects] == ["/mine"]
    comparison = scoped_client.get("/api/insights/machine-comparison" + requested).json()
    assert [m["machine_id"] for m in comparison["machines"]] == ["mine"]
    assert comparison["machines"][0]["total_cost"] == 1


def test_global_project_health_is_admin_only(scoped_client, database):
    assert scoped_client.get("/api/analytics/project-health?working_directory=/other").status_code == 403
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    response = scoped_client.get("/api/analytics/project-health?working_directory=/other")
    assert response.status_code == 200
    assert response.json()["overall_health_score"] == 90
    assert scoped_client.get("/api/stats/overview").json()["total_tokens"] == 910
    assert scoped_client.get("/api/stats/overview?machine_id=mine,other").json()["total_tokens"] == 910
    assert scoped_client.get("/api/stats/overview?machine_id=mine").json()["total_tokens"] == 10


@pytest.mark.parametrize("method,url,payload", [
    ("POST", "/api/groups", {"name": "New team"}),
    ("PUT", "/api/groups/1", {"name": "Changed team"}),
    ("DELETE", "/api/groups/1", None),
])
def test_team_mutations_require_admin(scoped_client, database, method, url, payload):
    assert scoped_client.request(method, url, json=payload).status_code == 403
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT name FROM groups").fetchall() == [("Engineering",)]
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    assert scoped_client.request(method, url, json=payload).status_code == 200


def test_sessions_list_renders_with_null_working_directory(scoped_client, database):
    """Regression test: working_directory is a nullable column (e.g. a
    machine that only ever sent a heartbeat), and the sessions list page
    used to 500 (TypeError: object of type 'NoneType' has no len()) from
    Jinja's `truncate` filter choking on None instead of rendering a blank
    cell."""
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, working_directory)
               VALUES ('no-workdir', 'mine', CURRENT_TIMESTAMP, NULL)"""
        )
    response = scoped_client.get("/sessions")
    assert response.status_code == 200
    assert "no-workdir" in response.text
