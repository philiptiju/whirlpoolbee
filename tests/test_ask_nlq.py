"""Integration regression tests for the Ask page's natural-language query
feature (database scoping + /api/query endpoint), covering the real-world
failures discovered in production logs:
  - "me"/"I" questions always failed because machines.logged_in_user is
    stored as 'Name <email>', not a bare username, and the old code
    compared that raw string directly against the resolved caller email.
  - Per-user matching (e.g. asking about a specific person by name) never
    matched for the same reason.
  - "Who spent the most" / "leaderboard" style ranking questions had no
    support at all -- they'd either fail outright or (worse) silently
    return one combined total for the whole org instead of a per-person
    breakdown.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def nlq_client(api_app):
    with TestClient(api_app) as client:
        yield client


@pytest.fixture
def admin_nlq_client(api_app, database):
    """Ranking/org-wide questions are inherently cross-user, so (like the
    Azure DevOps admin page) they only make sense for an admin caller --
    a regular user is restricted to their own machine(s) by middleware
    unrelated to this feature."""
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    with TestClient(api_app) as client:
        yield client



@pytest.mark.asyncio
async def test_nlq_known_users_returns_normalized_local_parts(database):
    now = datetime.utcnow()
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Mac', 'Owner Name <owner@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m2', 'PC', 'bob', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.commit()

    assert await database.nlq_known_users() == ["bob", "owner"]


@pytest.mark.asyncio
async def test_me_question_resolves_despite_name_angle_bracket_email_format(nlq_client, database):
    """Regression test for the exact bug reported in production: 'How many
    sessions did I have this month?' always answered 'Couldn't determine
    who you are', because the caller's email local part ('owner') was
    compared for exact equality against the raw stored identity string
    ('Owner Name <owner@example.com>')."""
    now = datetime.utcnow()
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Mac', 'Owner Name <owner@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        for i in range(3):
            conn.execute(
                """INSERT INTO sessions (session_id, machine_id, start_time, end_time, duration_seconds)
                   VALUES (?, 'm1', ?, ?, 600)""",
                (f"sess-{i}", now.isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )
        conn.commit()

    # api_app's auth_utils.current_user is monkeypatched to this exact email.
    resp = nlq_client.post("/api/query", json={"question": "How many sessions did I have this month?"})
    body = resp.json()
    assert body["matched"] is True, body
    assert body["result"]["value"] == 3


@pytest.mark.asyncio
async def test_ranking_question_merges_same_person_across_raw_identity_formats(admin_nlq_client, database):
    """Regression test: a person whose logged_in_user was captured
    differently on two machines (e.g. after re-logging-in, or syncing from
    an older agent version) must still be counted as one person in a
    ranking/leaderboard answer, not split into two rows."""
    now = datetime.utcnow()
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Mac', 'Alice Smith <alice@example-corp.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m2', 'PC', 'alice@example.com', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m3', 'Linux', 'bob', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        for mid, cost in [("m1", 10.0), ("m2", 15.0), ("m3", 5.0)]:
            conn.execute(
                """INSERT INTO token_usage (timestamp, session_id, machine_id, total_tokens, estimated_cost_usd)
                   VALUES (?, ?, ?, 1000, ?)""",
                (now.isoformat(), f"sess-{mid}", mid, cost),
            )
        conn.commit()

    resp = admin_nlq_client.post("/api/query", json={"question": "Who is spending the most this month?"})
    body = resp.json()
    assert body["matched"] is True, body
    breakdown = body["result"]["breakdown"]
    assert body["result"]["is_ranking"] is True
    # alice's two machines (10 + 15) must be merged into a single row.
    assert breakdown[0] == {"label": "alice", "value": 25.0}
    assert breakdown[1] == {"label": "bob", "value": 5.0}


@pytest.mark.asyncio
async def test_ranking_question_without_metric_defaults_to_cost(admin_nlq_client, database):
    now = datetime.utcnow()
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Mac', 'bob', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO token_usage (timestamp, session_id, machine_id, total_tokens, estimated_cost_usd)
               VALUES (?, 'sess-1', 'm1', 1000, 7.5)""",
            (now.isoformat(),),
        )
        conn.commit()

    resp = admin_nlq_client.post("/api/query", json={"question": "Who is the top in the leaderboard"})
    body = resp.json()
    assert body["matched"] is True, body
    assert body["metric"] == "cost"
    assert body["result"]["breakdown"][0] == {"label": "bob", "value": 7.5}


def test_unmatched_question_suggests_real_examples_not_fictitious_team(admin_nlq_client, database):
    """Regression test: the old hardcoded example 'Team Alpha' would itself
    fail in any deployment without a team literally named Alpha. Suggested
    follow-ups must come from teams that actually exist."""
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO groups (name) VALUES ('BB')")
        conn.commit()

    resp = admin_nlq_client.post("/api/query", json={"question": "What's the weather today?"})
    body = resp.json()
    assert body["matched"] is False
    assert any("BB" in ex for ex in body["examples"])
    assert not any("Alpha" in ex for ex in body["examples"])
