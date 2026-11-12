"""Shared UI assets must change URL when their deployed content changes."""

import hashlib
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    from src.utils.config import Config

    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(Config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "AUTH_OVERRIDE", False)
    from src.api import main

    monkeypatch.setattr(main, "db", main.Database(str(Config.DB_PATH)))
    return main


@pytest.mark.parametrize("root_path", ["", "/app/claude/v9"])
@pytest.mark.parametrize("is_admin", [True, False])
def test_shared_assets_have_content_versioned_urls(web_app, root_path, is_admin):
    request = SimpleNamespace(
        state=SimpleNamespace(
            root_path=root_path, is_admin=is_admin, is_super_user=False
        ),
        url=SimpleNamespace(path=root_path + "/"),
        session={},
    )
    html = web_app.templates.get_template("base.html").render(request=request)
    assets = re.findall(
        r'(?:href|src)="([^"]*/static/(?:css|js)/[^"]+)"', html
    )
    assert len(assets) == 4
    assert ('id="machineTopbar"' in html) == is_admin

    with TestClient(web_app.app) as client:
        for url in assets:
            assert url.startswith(root_path + "/static/")
            relative_path, version = url.split("/static/", 1)[1].split("?v=")
            content = (web_app.static_dir / relative_path).read_bytes()
            assert version == hashlib.sha256(content).hexdigest()[:16]
            # The ingress strips root_path before forwarding to StaticFiles.
            response = client.get(url[len(root_path):])
            assert response.status_code == 200
            assert response.content == content
