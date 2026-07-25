from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, so the backend picks up the shared .env whether it is started from
# the repo root or from backend/.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Aliased fields still have to be constructible by field name in tests.
        populate_by_name=True,
    )

    env: Literal["dev", "test", "production"] = "dev"

    # OpenAI
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # Dated snapshots, never the floating alias: a silent model swap changes
    # extraction quality with no diff to point at.
    llm_model_extract: str = "gpt-5.5-2026-04-23"
    llm_model_report: str = "gpt-5.5-2026-04-23"

    # Database
    database_url: str | None = None
    test_database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PENNY_TEST_DATABASE_URL", "TEST_DATABASE_URL"),
    )

    # Auth — session cookie carrying household_id
    session_secret: str | None = None
    session_max_age_days: int = 30

    # GOWA WhatsApp sidecar
    gowa_url: str | None = None
    gowa_basic_auth: str | None = None  # "user:pass"
    whatsapp_webhook_secret: str | None = None

    # Shared secret for the Railway cron service calling /api/internal/tick
    internal_tick_secret: str | None = None

    # Spend guards
    import_max_spend_usd: Decimal = Decimal("25")
    llm_monthly_budget_usd_per_household: Decimal = Decimal("15")

    default_timezone: str = "Europe/London"
    serve_frontend: bool = True

    # --- Group onboarding -----------------------------------------------------------------
    # Where a family is sent after Penny is added to their group. It goes into the welcome
    # message, so a wrong value here is a dead link in the first thing the product ever says.
    app_public_url: str = "https://pennyai.chat"
    # The kill switch. False makes an unknown group a silent 200 again, which is the pre-
    # onboarding behaviour and the thing to reach for if the WhatsApp number gets out.
    onboarding_enabled: bool = True
    # The blast radius of open onboarding is the OpenAI bill, not data: anyone who learns the
    # number can mint households, and every household runs extraction. This cap is what stops
    # that starving real families of their budget.
    onboarding_max_households: int = 25
    # households.care_recipient_name is NOT NULL and at provision time nobody has told us who
    # is being cared for. "Still the placeholder" is the signal that first-run setup is needed,
    # so this string is compared against, not just displayed — change it and every existing
    # household looks configured.
    onboarding_placeholder_care_recipient: str = "your family member"

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: str | None) -> str | None:
        """Railway hands out postgresql://; asyncpg needs the driver in the scheme."""
        if value is None:
            return None
        for scheme in ("postgres://", "postgresql://"):
            if value.startswith(scheme):
                return "postgresql+asyncpg://" + value[len(scheme) :]
        return value

    @property
    def sync_database_url(self) -> str | None:
        """Same URL for tools that speak psycopg/DBAPI rather than asyncpg (e.g. Alembic)."""
        if self.database_url is None:
            return None
        return self.database_url.replace("+asyncpg", "", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
