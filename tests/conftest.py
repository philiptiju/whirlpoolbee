import pytest

from src.database import Database
from src.utils.config import Config


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "regression.db"))
    db.initialize()
    return db


@pytest.fixture
def api_app(database, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(Config, "DB_PATH", database.db_path)
    monkeypatch.setattr(Config, "AUTH_OVERRIDE", True)
    from src.api import main

    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main.app.state, "db", database)
    monkeypatch.setattr(main, "ROOT_PATH", "")
    monkeypatch.setattr(
        main.auth_utils, "current_user", lambda request: {"email": "owner@example.com"}
    )
    return main.app
