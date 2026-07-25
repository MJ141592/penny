"""The one thing here that cannot be proved by booting the app once: that `.env.example` and
`PUBLISHED_PLACEHOLDERS` stay in sync.

Everything else in `startup_checks.py` fails loudly the moment it is wrong. This does not: add a
new secret to `.env.example`, forget the constant, and the validator goes on passing while the
published value it was written to catch sails through. That is exactly the bug this finding was.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.startup_checks import (
    MIN_SECRET_CHARS,
    PUBLISHED_PLACEHOLDERS,
    StartupCheckError,
    check_production_settings,
    enforce_startup_checks,
)

ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The variables in `.env.example` that carry a credential, and the Settings field each one feeds.
# PENNY_TEST_DATABASE_URL is deliberately absent: it is only read by the test suite and never by
# a production process, so there is nothing for a production check to say about it.
SECRET_VARS: dict[str, str] = {
    "SESSION_SECRET": "session_secret",
    "WHATSAPP_WEBHOOK_SECRET": "whatsapp_webhook_secret",
    "INTERNAL_TICK_SECRET": "internal_tick_secret",
    "GOWA_BASIC_AUTH": "gowa_basic_auth",
    "OPENAI_API_KEY": "openai_api_key",
    "DATABASE_URL": "database_url",
}


def _env_example_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _safe_settings(**overrides: object) -> Settings:
    """Settings a real production deploy could plausibly have: nothing published, nothing short."""
    fields: dict[str, object] = {
        "env": "production",
        "session_secret": secrets.token_hex(32),
        "database_url": "postgresql://real:pw@db.railway.internal:5432/railway",
        "whatsapp_webhook_secret": secrets.token_hex(32),
        "internal_tick_secret": secrets.token_hex(32),
        "gowa_basic_auth": f"penny:{secrets.token_hex(32)}",
        "openai_api_key": "sk-proj-" + secrets.token_hex(24),
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


def test_every_secret_placeholder_in_env_example_is_rejected() -> None:
    """The sync check. A placeholder added to `.env.example` and not to `PUBLISHED_PLACEHOLDERS`
    fails here, which is the only place it would ever be noticed before a deploy.
    """
    published = _env_example_values(ENV_EXAMPLE)
    checked = 0

    for env_var, field in SECRET_VARS.items():
        assert env_var in published, f"{env_var} vanished from .env.example; update SECRET_VARS"
        problems = check_production_settings(_safe_settings(**{field: published[env_var]}))
        assert any(p.startswith(env_var) for p in problems), (
            f"{env_var}={published[env_var]!r} is published in .env.example but "
            f"check_production_settings accepts it. Add it to PUBLISHED_PLACEHOLDERS."
        )
        checked += 1

    assert checked == len(SECRET_VARS)


def test_baseline_production_settings_are_accepted() -> None:
    """Without this the test above passes trivially if the validator rejects everything."""
    assert check_production_settings(_safe_settings()) == []


@pytest.mark.parametrize(
    ("field", "value", "expected_var"),
    [
        ("session_secret", None, "SESSION_SECRET"),
        ("database_url", None, "DATABASE_URL"),
        ("session_secret", "x" * (MIN_SECRET_CHARS - 1), "SESSION_SECRET"),
        ("whatsapp_webhook_secret", "x" * (MIN_SECRET_CHARS - 1), "WHATSAPP_WEBHOOK_SECRET"),
        ("internal_tick_secret", "x" * (MIN_SECRET_CHARS - 1), "INTERNAL_TICK_SECRET"),
        # GOWA's own defaults, on both sides.
        ("whatsapp_webhook_secret", "secret", "WHATSAPP_WEBHOOK_SECRET"),
        ("gowa_basic_auth", "penny:secret", "GOWA_BASIC_AUTH"),
        # GOWA splits APP_BASIC_AUTH on ":" and exits unless it gets exactly two parts.
        ("gowa_basic_auth", "penny:pa:ss", "GOWA_BASIC_AUTH"),
        ("gowa_basic_auth", "nocolon", "GOWA_BASIC_AUTH"),
        # The specific string this finding was filed about.
        ("session_secret", "change-me-64-hex-chars", "SESSION_SECRET"),
    ],
)
def test_unsafe_values_are_rejected_and_name_their_variable(
    field: str, value: str | None, expected_var: str
) -> None:
    problems = check_production_settings(_safe_settings(**{field: value}))
    assert [p for p in problems if p.startswith(expected_var)], problems


def test_no_problem_message_echoes_the_value() -> None:
    """A refused boot is written to Railway's log stream, which is not a place a secret goes."""
    leaky = "Zg7-must-not-be-logged"  # short, so the length rule fires on it too
    settings = _safe_settings(
        session_secret=leaky,
        whatsapp_webhook_secret=leaky,
        internal_tick_secret=leaky,
        gowa_basic_auth=f"penny:{leaky}:extra",
        database_url=leaky,
    )
    problems = check_production_settings(settings)
    assert problems, "expected these settings to be rejected"
    assert not any(leaky in problem for problem in problems)


def test_production_refuses_to_boot_and_dev_only_warns() -> None:
    """The asymmetry IS the design: fail closed where it matters, warn where breaking a first run
    would just teach people to delete the check.
    """
    unsafe = {"session_secret": "change-me-64-hex-chars"}

    with pytest.raises(StartupCheckError) as excinfo:
        enforce_startup_checks(_safe_settings(**unsafe))
    assert "SESSION_SECRET" in str(excinfo.value)
    assert "change-me-64-hex-chars" not in str(excinfo.value)

    for env in ("dev", "test"):
        problems = enforce_startup_checks(_safe_settings(env=env, **unsafe))
        assert any(p.startswith("SESSION_SECRET") for p in problems)


def test_a_clean_production_config_boots() -> None:
    assert enforce_startup_checks(_safe_settings()) == []


def test_the_compose_database_url_is_rejected_after_the_asyncpg_rewrite() -> None:
    """`Settings` rewrites postgresql:// to postgresql+asyncpg://, so a naive equality check
    against the `.env.example` line would never fire.
    """
    settings = _safe_settings(database_url="postgresql://penny:penny@localhost:5432/penny")
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert any(p.startswith("DATABASE_URL") for p in check_production_settings(settings))


def test_optional_secrets_may_be_unset() -> None:
    """House rule: the app boots and serves the feed with GOWA unreachable and no OpenAI key."""
    settings = _safe_settings(
        whatsapp_webhook_secret=None,
        internal_tick_secret=None,
        gowa_basic_auth=None,
        openai_api_key=None,
    )
    assert check_production_settings(settings) == []


def test_the_placeholder_list_is_a_constant_not_a_file_read() -> None:
    """`.dockerignore` excludes `**/.env.*`, so `.env.example` is absent from the runtime image.
    A validator that parsed it at boot would find nothing and pass — silently, in the one
    environment it exists to protect. So it has to stay a literal constant.
    """
    assert "change-me-64-hex-chars" in PUBLISHED_PLACEHOLDERS
    source = (REPO_ROOT / "backend" / "app" / "startup_checks.py").read_text(encoding="utf-8")
    for reader in ("read_text(", "open(", "load_dotenv", "os.environ"):
        assert reader not in source, f"startup_checks.py must not use {reader}"
