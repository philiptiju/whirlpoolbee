import asyncio
import json
import sqlite3
from datetime import date

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("machine_id", ["machine-a", None])
async def test_model_performance_concurrent_updates(database, machine_id):
    async def report(i):
        await database.upsert_model_performance(
            "test-model", "general", date.today(), response_time_ms=i * 10,
            success=i % 2 == 0, token_efficiency=i, cost=i / 100,
            machine_id=machine_id,
        )
    await asyncio.gather(*(report(i) for i in range(20)))
    rows = await database.get_model_performance()
    assert len(rows) == 1
    assert rows[0]["total_tasks"] == 20
    assert rows[0]["success_rate"] == pytest.approx(50)
    assert rows[0]["average_response_time_ms"] == pytest.approx(95)
    assert rows[0]["average_token_efficiency"] == pytest.approx(9.5)
    assert rows[0]["cost_per_task"] == pytest.approx(0.095)


@pytest.mark.asyncio
async def test_open_sessions_do_not_earn_completion_credit(database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time)
               VALUES ('open-session', 'mine', CURRENT_TIMESTAMP)"""
        )
    async with database.get_connection() as conn:
        stats = await database._get_user_activity_stats(conn, "mine")
    assert stats["sessions_completed"] == 0
    await database.recompute_user_achievements("mine")
    assert await database.get_user_achievements("mine") == []
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("UPDATE sessions SET end_time = CURRENT_TIMESTAMP")
    async with database.get_connection() as conn:
        stats = await database._get_user_activity_stats(conn, "mine")
    assert stats["sessions_completed"] == 1
    await database.recompute_user_achievements("mine")
    assert len(await database.get_user_achievements("mine")) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("level,steps", [(0, 0), (33, 0), (34, 1), (66, 1), (67, 2), (99, 2), (100, 3)])
async def test_learning_path_reaches_completion(database, level, steps):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, end_time)
               VALUES ('session', 'mine', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
        conn.execute(
            "INSERT INTO prompt_analysis (session_id, prompt_text, quality_score) VALUES ('session', 'Prompt', ?)",
            (level,),
        )
    await database.recompute_learning_path_progress("mine")
    paths = await database.get_user_learning_progress("mine")
    path = next(p for p in paths if p["name"] == "Effective Prompting Fundamentals")
    assert path["current_step"] == steps
    assert json.loads(path["steps_completed"]) == list(range(1, steps + 1))
    assert path["progress_percentage"] == round(steps / 3 * 100, 1)
    assert bool(path["completed_at"]) == (steps == 3)


@pytest.mark.asyncio
async def test_project_duration_is_not_multiplied_by_token_events(database):
    with sqlite3.connect(database.db_path) as conn:
        for i in range(2):
            conn.execute(
                """INSERT INTO sessions (session_id, machine_id, start_time, working_directory, duration_seconds)
                   VALUES (?, 'mine', CURRENT_TIMESTAMP, '/project', 3600)""",
                (f"session-{i}",),
            )
        for cost in [1, 2, 3]:
            conn.execute(
                """INSERT INTO token_usage (session_id, machine_id, timestamp, estimated_cost_usd)
                   VALUES ('session-0', 'mine', CURRENT_TIMESTAMP, ?)""",
                (cost,),
            )
    project = (await database.get_top_projects(machine_id="mine"))[0]
    assert project["session_count"] == 2
    assert project["total_hours"] == 2
    assert project["total_cost"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2, 3])
async def test_derived_practices_respect_limit(database, limit):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time)
               VALUES ('session', 'mine', CURRENT_TIMESTAMP)"""
        )
        conn.execute(
            """INSERT INTO prompt_analysis (session_id, prompt_text, quality_score, suggestions)
               VALUES ('session', 'Prompt', 90, '["Be specific"]')"""
        )
    practices = await database.get_derived_best_practices("mine", limit=limit)
    assert len(practices) == min(limit, 2)
    with pytest.raises(ValueError):
        await database.get_derived_best_practices("mine", limit=-1)
