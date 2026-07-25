import sys
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

import app.config
from app.config import Settings, get_settings
from app.main import app as penny_app
from app.openai_client import get_openai_client


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Settings and the OpenAI client are lru_cached process-wide.

    Clearing before AND after means no test inherits another's environment and
    no test leaks its own — so individual tests never have to think about it.
    """
    get_settings.cache_clear()
    get_openai_client.cache_clear()
    yield
    get_settings.cache_clear()
    get_openai_client.cache_clear()


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Swap in a Settings built from kwargs, ignoring .env entirely.

    Modules bind `get_settings` at import time, so patching app.config alone would
    leave every importer still calling the real one; patch each binding too.
    """

    def _override(**kwargs: object) -> Settings:
        settings = Settings(_env_file=None, **kwargs)
        # Bound to a LOCAL before any patching. `get_settings` as a global would be re-read on
        # every iteration, and this module's own binding is one of the ones about to be
        # patched — so the comparison target would silently become the lambda after the first
        # match and every module later in sys.modules would be skipped.
        real = get_settings
        monkeypatch.setattr(app.config, "get_settings", lambda: settings)
        for module in list(sys.modules.values()):
            if getattr(module, "get_settings", None) is real:
                monkeypatch.setattr(module, "get_settings", lambda: settings)
        return settings

    return _override


@pytest.fixture
def client() -> TestClient:
    return TestClient(penny_app)


@pytest.fixture
def db_url() -> str:
    """Real-Postgres tests opt in via env, mirroring the RUN_LIVE_OPENAI_TESTS pattern."""
    url = get_settings().test_database_url
    if not url:
        pytest.skip("set PENNY_TEST_DATABASE_URL to run DB tests")
    return url
