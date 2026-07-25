"""Fail-closed validation of the secrets a production deploy is running on.

`config.py` only checks that a value is *present*. Presence is not enough: `.env.example` is a
file in a public repository, so every literal in it is a published string. A deploy that never
set `SESSION_SECRET` and inherited `change-me-64-hex-chars` is one `git clone` away from anyone
forging a session cookie for any household. This module is the check that turns that from a
silent success into a refused boot.

Two rules shape everything here.

FAIL CLOSED, BUT ONLY IN PRODUCTION. `check_production_settings` is pure and env-agnostic: it
answers "would these settings be safe in production?" and returns the problems. The wrapper,
`enforce_startup_checks`, is the one that looks at `settings.env` and decides whether a problem
is fatal or merely a warning. In dev it warns, because a contributor's first `docker compose up`
must not be a stack trace.

THE PLACEHOLDER LIST IS A CONSTANT, NOT A FILE READ. `.dockerignore` excludes `**/.env.*`, so
`.env.example` is not in the runtime image — parsing it at boot would silently find nothing in
the exact environment this check exists to protect. So the placeholders are spelled out below.
`tests/test_startup_checks.py` reads the real `.env.example` and asserts every secret in it is
rejected, which is what keeps the constant honest when someone adds a variable.

Wiring (main.py owns the line, after `configure_logging()` so warnings are already JSON):

    enforce_startup_checks(settings)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

# Long enough that a 128-bit secret passes and a human-typed word does not. `token_hex(32)`
# gives 64 characters, so the documented way of generating one clears this with room to spare.
MIN_SECRET_CHARS = 32

# Every secret-shaped literal that has ever appeared in `.env.example`, current and historical.
# Historical entries stay forever: the whole point is catching a deploy that was configured
# from an older copy of the file and never revisited.
PUBLISHED_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        # The originals, and the reason this module exists.
        "change-me-64-hex-chars",
        "change-me",
        "penny:change-me",
        "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        # Current `.env.example` values. Deliberately short as well as listed, so they fail the
        # length rule too even if someone edits this set without thinking.
        "REPLACE_ME",
        "penny:REPLACE_ME",
        "sk-REPLACE_ME",
        # The local docker-compose database, published credentials and all. `.env.example` keeps
        # it because it is genuinely useful for dev; production must never boot on it.
        "postgresql://penny:penny@localhost:5432/penny",
        "postgresql://penny:penny@localhost:5432/penny_test",
        # Not from `.env.example`, but the same class of thing: GOWA's own shipped default, and
        # the spelling `webhooks.FORBIDDEN_SECRETS` already rejects at request time.
        "secret",
        "changeme",
    }
)

# GOWA's `APP_BASIC_AUTH` and `WHATSAPP_WEBHOOK_SECRET` both ship as "secret".
GOWA_DEFAULT_SECRET = "secret"

_GENERATE = 'generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
_SET_IT = "Set it in the Railway service variables"


@dataclass(frozen=True, slots=True)
class _SecretRule:
    """One environment variable and what production demands of it."""

    env_var: str
    attr: str
    required: bool = False
    min_chars: int = 0


# Only SESSION_SECRET and DATABASE_URL are required — the rest are features that can legitimately
# be switched off by leaving them unset (house rule: the app boots and serves the feed with GOWA
# unreachable and no OpenAI key). But if a value IS set, it must be a real one.
_SECRET_RULES: tuple[_SecretRule, ...] = (
    _SecretRule("SESSION_SECRET", "session_secret", required=True, min_chars=MIN_SECRET_CHARS),
    _SecretRule("DATABASE_URL", "database_url", required=True),
    _SecretRule("WHATSAPP_WEBHOOK_SECRET", "whatsapp_webhook_secret", min_chars=MIN_SECRET_CHARS),
    _SecretRule("INTERNAL_TICK_SECRET", "internal_tick_secret", min_chars=MIN_SECRET_CHARS),
    _SecretRule("GOWA_BASIC_AUTH", "gowa_basic_auth"),
    _SecretRule("OPENAI_API_KEY", "openai_api_key"),
)

_FIX_ADVICE: dict[str, str] = {
    "SESSION_SECRET": f"{_SET_IT} and {_GENERATE}. Rotating it logs every household out.",
    "DATABASE_URL": f"{_SET_IT} to the managed Postgres URL Railway provides.",
    "WHATSAPP_WEBHOOK_SECRET": (
        f"{_SET_IT} and {_GENERATE}, then set the same value as WHATSAPP_WEBHOOK_SECRET on the "
        "GOWA service — the webhook is unauthenticated until both sides match."
    ),
    "INTERNAL_TICK_SECRET": (
        f"{_SET_IT} and {_GENERATE}, then set the same value on the cron service that POSTs "
        "/api/internal/tick."
    ),
    "GOWA_BASIC_AUTH": (
        f'{_SET_IT} as "user:pass" and {_GENERATE} for the password, then set the same pair as '
        "APP_BASIC_AUTH on the GOWA service."
    ),
    "OPENAI_API_KEY": (
        f"{_SET_IT} to a real key from platform.openai.com, or unset it entirely to run with "
        "extraction disabled."
    ),
}


def _placeholder_forms(env_var: str, value: str) -> tuple[str, ...]:
    """Every spelling of `value` worth comparing against the published placeholders.

    Two settings do not reach us as they were typed. `Settings._require_asyncpg_driver` rewrites
    `postgresql://` to `postgresql+asyncpg://`, so a literal comparison against the `.env.example`
    line would never match. And `GOWA_BASIC_AUTH` is a "user:pass" pair, where the half that
    matters is the password.
    """
    forms = [value]
    if env_var == "DATABASE_URL":
        forms.append(value.replace("+asyncpg", "", 1))
    if env_var == "GOWA_BASIC_AUTH" and ":" in value:
        forms.append(value.partition(":")[2])
    return tuple(forms)


# Values that deserve a more specific sentence than "it is in .env.example", keyed by
# (variable, lowercased value). GOWA ships both of these as its own defaults, so they are not
# placeholders somebody forgot to change — they are defaults they may never have seen.
_KNOWN_BAD_VALUES: dict[tuple[str, str], str] = {
    ("WHATSAPP_WEBHOOK_SECRET", GOWA_DEFAULT_SECRET): (
        "WHATSAPP_WEBHOOK_SECRET is GOWA's shipped default, so the inbound webhook is "
        "effectively unauthenticated."
    ),
    ("GOWA_BASIC_AUTH", GOWA_DEFAULT_SECRET): (
        "GOWA_BASIC_AUTH uses GOWA's shipped default password, so anyone who can reach the "
        "sidecar can send WhatsApp messages as this number."
    ),
}


def _problem_for(rule: _SecretRule, value: str | None) -> str | None:
    """The first thing wrong with one variable, or None. First only: a placeholder that is also
    too short is one mistake with one fix, and two lines for it just buries the other variables.
    """
    advice = _FIX_ADVICE[rule.env_var]
    stripped = (value or "").strip()

    if not stripped:
        if rule.required:
            return f"{rule.env_var} is not set. {advice}"
        return None

    forms = _placeholder_forms(rule.env_var, stripped)

    for form in forms:
        if (known_bad := _KNOWN_BAD_VALUES.get((rule.env_var, form.lower()))) is not None:
            return f"{known_bad} {advice}"

    if any(form in PUBLISHED_PLACEHOLDERS for form in forms):
        return (
            f"{rule.env_var} is still a placeholder published in this repository's .env.example, "
            f"so it is public. {advice}"
        )

    if rule.min_chars and len(stripped) < rule.min_chars:
        return (
            f"{rule.env_var} is shorter than {rule.min_chars} characters and is too weak to "
            f"sign or verify anything. {advice}"
        )

    return None


def _gowa_basic_auth_shape_problem(value: str | None) -> str | None:
    """GOWA parses `APP_BASIC_AUTH` by splitting on ":" and `Fatalln`s unless it gets exactly two
    parts, so a password containing a colon takes the sidecar down at ITS startup, minutes after
    ours came up clean. Catching it here turns a mysterious dead sidecar into a boot message.
    """
    stripped = (value or "").strip()
    if not stripped:
        return None

    parts = stripped.split(":")
    if len(parts) == 2:
        return None
    return (
        f'GOWA_BASIC_AUTH must be exactly "user:pass" — it splits into {len(parts)} parts on '
        '":", and GOWA exits at startup unless it gets exactly two. '
        f"{_FIX_ADVICE['GOWA_BASIC_AUTH']}"
    )


def check_production_settings(settings: Settings) -> list[str]:
    """Everything that would be unsafe about running THESE settings in production.

    Env-agnostic on purpose: it answers the same question whatever `settings.env` says, which is
    what makes it testable without pretending to be deployed. `enforce_startup_checks` is the
    caller that decides whether the answer is fatal.

    Every string names its variable and says what to do. None of them contains a value — a boot
    failure is written to Railway's log stream, which is not a place a secret should ever exist.
    """
    problems = [
        problem
        for rule in _SECRET_RULES
        if (problem := _problem_for(rule, getattr(settings, rule.attr, None))) is not None
    ]

    # Shape, not strength: the "user:pass" pair has a failure mode no per-value rule catches.
    if (shape := _gowa_basic_auth_shape_problem(settings.gowa_basic_auth)) is not None:
        problems.append(shape)

    return problems


class StartupCheckError(RuntimeError):
    """Production settings are unsafe. Boot stops here, deliberately.

    A site that is down is recoverable in one redeploy. A site running on a published secret is
    a health record anyone can read, and nothing about it looks wrong from the outside.
    """


def enforce_startup_checks(settings: Settings) -> list[str]:
    """Raise in production if anything is wrong; warn everywhere else. Returns the problems.

    Called once from `main.py` at import time, before the first request can be served.
    """
    problems = check_production_settings(settings)

    if not problems:
        log.info("startup_checks.ok", extra={"app_env": settings.env})
        return problems

    if settings.env != "production":
        # Not fatal here: `.env.example` is exactly what a contributor's first run is built from,
        # and breaking that teaches people to delete the check.
        log.warning(
            "startup_checks.would_fail_in_production",
            extra={"app_env": settings.env, "problem_count": len(problems)},
        )
        for problem in problems:
            log.warning("startup_checks.problem", extra={"detail": problem})
        return problems

    listed = "\n".join(f"  - {problem}" for problem in problems)
    raise StartupCheckError(
        f"Refusing to boot: {len(problems)} unsafe setting(s) with ENV=production.\n"
        f"{listed}\n"
        "Fix these in the Railway service variables and redeploy. No values are printed above; "
        "check them in the dashboard, and rotate anything that was ever deployed as a placeholder."
    )
