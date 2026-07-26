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

    # --- Voice note transcription ---------------------------------------------------------
    # In a family care chat "Mum had a fall this morning" is often SPOKEN. Without this the
    # message reaches the timeline as "[voice note]" and extraction sees nothing at all.
    #
    # The kill switch. False leaves every voice note as the placeholder — the pre-transcription
    # behaviour, and the thing to reach for if the audio bill or the sidecar misbehaves.
    transcribe_voice_notes: bool = True
    # The cheap model, deliberately: a 20-second voice note is a small job. Raise it to
    # "gpt-4o-transcribe" if accuracy on names and doses is not good enough. Every value here
    # must have a per-minute price in `app.llm.pricing.AUDIO_PRICES_PER_MINUTE`, or
    # transcription refuses to run rather than spending money it cannot account for.
    transcription_model: str = "gpt-4o-mini-transcribe"
    # A 40-minute audio file in a family chat is somebody forwarding a podcast, not care
    # signal — and it is the shape that quietly costs real money (300s at the mini rate is
    # $0.015; 40 minutes is $0.12, every time it is replayed). Duration is read from the GOWA
    # payload, so a note whose duration is not declared is not refused by this gate; the byte
    # cap below is the backstop for that case.
    transcription_max_seconds: int = 300
    # Hard cap on what we will pull from the sidecar into memory. A one-minute WhatsApp voice
    # note is well under 200 KB, so 20 MB is far past any real one and exists only so a
    # misbehaving (or hostile) media response cannot be unbounded.
    transcription_max_bytes: int = 20 * 1024 * 1024

    default_timezone: str = "Europe/London"
    serve_frontend: bool = True

    # --- Extraction cadence ---------------------------------------------------------------
    # Extraction is charged per CALL, not per message: ~1,400 tokens of system prompt, care
    # brief and open-events block ride along with every request regardless of how much
    # conversation is in it. Extracting on every inbound message measured at $0.00763/message
    # against a planned $0.00042 — 18x — because a five-message run pays that fixed tax
    # thirteen times over. Batching also extracts BETTER: a thread about one appointment
    # arrives finished and becomes one event instead of three partials that then need paid
    # merge calls to reconcile.
    #
    # These two are OR-ed, and the gate lives in `extraction.service` so that every caller
    # inherits it. Raising the count saves money and adds latency to the feed; the age is what
    # bounds that latency, so lowering the count without also lowering the age is the safe
    # direction to experiment in.
    extract_min_unextracted: int = 40
    # NOT an int: tests and a "flush fast" incident setting both want fractions of an hour.
    # The age is measured from the oldest waiting message's `sent_at`.
    extract_max_age_hours: float = 6.0

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
    # How long after process start a `group.joined` burst is treated as whatsmeow app-state
    # sync rather than a human adding Penny to a chat. whatsmeow re-emits JoinedGroup for every
    # group the account was ALREADY in while it replays app state after a connect, and every
    # deploy reconnects — which is how a login password reached group chats nobody had invited
    # Penny to. Nothing on the event distinguishes the two cases; the only signal is that the
    # burst arrives seconds after the socket came up.
    #
    # A join refused in this window is still RECORDED, so it can never provision later either
    # (see `app.groups`). The cost is that a family who add Penny in the first three minutes
    # after a deploy get no welcome and a human sends their credentials by hand — a support
    # message, against a password in a stranger's chat, which cannot be unsent.
    #
    # 0 disables the window, which is only safe on a number that belongs to no other groups.
    startup_quiet_period_seconds: float = 180.0
    # The third gate, and the one that covers the day this ships. The other two can both be
    # open at once: on a brand-new ledger every pre-existing group is a "first sighting", and a
    # sync burst that lands a little more than `startup_quiet_period_seconds` after boot clears
    # the quiet window too. That combination was reproducible against the shipped code — eight
    # joins, eight households, eight passwords — and it is the exact state of production on the
    # first deploy, when `known_groups` is empty.
    #
    # The signal is cardinality: a human adds Penny to ONE group at a time, whereas whatsmeow
    # replays every group the account already belonged to at once. So a welcome is held for this
    # long and only sent if no other group appeared in the meantime. Every genuine welcome is
    # therefore ~45s late, which is the whole cost. 0 disables the guard.
    join_burst_window_seconds: float = 45.0

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
