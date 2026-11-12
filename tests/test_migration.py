import sqlite3

from src.utils.config import Config
import migrate_to_multi_machine as migration


def test_migration_rerun_preserves_machine_metadata(database, monkeypatch):
    monkeypatch.setattr(Config, "DB_PATH", database.db_path)
    monkeypatch.setattr(migration, "get_machine_id", lambda: "local")
    migration.migrate_database()
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """UPDATE machines SET first_seen='2020-01-01', last_seen='2026-01-01',
               logged_in_user='owner@example.com', agent_version='test',
               agent_token='test-token', is_active=0 WHERE machine_id='local'"""
        )
        expected = conn.execute("SELECT * FROM machines").fetchall()
    migration.migrate_database()
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT * FROM machines").fetchall() == expected
