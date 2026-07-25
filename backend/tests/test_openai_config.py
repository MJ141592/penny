from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.openai_client import OpenAINotConfiguredError, get_openai_client


def test_ai_status_reports_config_without_leaking_key(client: TestClient) -> None:
    response = client.get("/api/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"configured", "model"}
    assert isinstance(body["configured"], bool)
    assert body["model"] == get_settings().llm_model_extract


def test_client_raises_a_clear_error_when_key_is_missing(
    settings_override: Callable[..., Settings],
) -> None:
    settings_override(openai_api_key=None)
    with pytest.raises(OpenAINotConfiguredError):
        get_openai_client()


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()


def test_database_url_gets_the_asyncpg_driver(
    settings_override: Callable[..., Settings],
) -> None:
    """Railway hands out postgresql://; an already-correct URL must be left alone."""
    assert settings_override(database_url="postgres://u:p@h:5432/db").database_url == (
        "postgresql+asyncpg://u:p@h:5432/db"
    )
    assert settings_override(database_url="postgresql://u:p@h:5432/db").database_url == (
        "postgresql+asyncpg://u:p@h:5432/db"
    )
    settings = settings_override(database_url="postgresql+asyncpg://u:p@h:5432/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert settings.sync_database_url == "postgresql://u:p@h:5432/db"
