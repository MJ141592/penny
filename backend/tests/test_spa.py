from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.routers import health
from app.spa import mount_spa


@pytest.fixture
def built_spa(tmp_path: Path) -> Path:
    """A minimal frontend/dist: the shell plus one content-hashed asset."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Penny</title>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log('penny')")
    return tmp_path


def _build_app(
    monkeypatch: pytest.MonkeyPatch, static_dir: Path, *, serve_frontend: bool = True
) -> FastAPI:
    """Wire it exactly as main.py does: routers first, the SPA catch-all last.

    `app.spa` is patched directly rather than through the `settings_override` fixture,
    which cannot reach a module imported after conftest.
    """
    settings = Settings(_env_file=None, serve_frontend=serve_frontend)
    monkeypatch.setattr("app.spa.get_settings", lambda: settings)
    monkeypatch.setattr("app.spa.STATIC_DIRS", (static_dir,))

    app = FastAPI()
    app.include_router(health.router)
    mount_spa(app)
    return app


@pytest.fixture
def client(built_spa: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(_build_app(monkeypatch, built_spa))


def test_api_routes_still_win(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_api_path_is_a_json_404(client: TestClient) -> None:
    """Never index.html: an HTML body here becomes "Unexpected token '<'" in the client."""
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(response.json()["detail"], str)


def test_client_side_route_falls_back_to_index(client: TestClient) -> None:
    response = client.get("/events/9a1c-not-a-server-route")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Penny</title>" in response.text
    # A cached shell after a deploy points at asset URLs that no longer exist.
    assert response.headers["cache-control"] == "no-cache"


def test_root_serves_index(client: TestClient) -> None:
    assert "<title>Penny</title>" in client.get("/").text


def test_asset_is_served_verbatim(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log('penny')"


def test_traversal_cannot_escape_the_static_dir(client: TestClient) -> None:
    response = client.get("/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    assert response.status_code == 200
    assert "<title>Penny</title>" in response.text


def test_missing_build_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend-only dev loop has no dist/ — that must not take the API down."""
    client = TestClient(_build_app(monkeypatch, tmp_path / "never-built"))

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 404


def test_serve_frontend_off_is_a_noop(built_spa: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_build_app(monkeypatch, built_spa, serve_frontend=False))

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 404
