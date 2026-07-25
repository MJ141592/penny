import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.openai_client import OpenAINotConfiguredError, get_openai_client

client = TestClient(app)


def test_ai_status_reports_config_without_leaking_key() -> None:
    response = client.get("/api/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"configured", "model"}
    assert isinstance(body["configured"], bool)
    assert body["model"]


def test_client_raises_a_clear_error_when_key_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.openai_client.get_settings",
        lambda: Settings(_env_file=None, openai_api_key=None),
    )
    get_openai_client.cache_clear()
    with pytest.raises(OpenAINotConfiguredError):
        get_openai_client()
    get_openai_client.cache_clear()


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()
