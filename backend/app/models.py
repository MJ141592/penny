"""The whole schema, in one file.

Nine tables and no more. Two rules run through all of them:

1. **`household_id` is NOT NULL everywhere.** There is no RLS and no `users` table; tenant
   isolation is this column plus the signed session cookie plus one `HouseholdCtx` dependency.
   A query that forgets the filter is a tenant leak, so the column can never be absent to
   filter on.
2. **The indexes are the idempotency guarantees**, not performance tuning. The partial unique
   indexes on `messages` are what make a replayed GOWA webhook and a re-uploaded (longer)
   export insert exactly once. Read `__table_args__` as business logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import available_timezones

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

# Deterministic constraint names so autogenerate emits stable, droppable DDL. Without this,
# Postgres invents names for CHECK/FK constraints and a later migration cannot address them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EVENT_KINDS = ("symptom", "appointment", "medication", "note")
OCCURRED_AT_PRECISIONS = ("exact", "day", "week", "month", "unknown")
PROVIDERS = ("gowa", "whatsapp_export", "manual")
IMPORT_STATUSES = ("pending", "importing", "extracting", "complete", "failed")
REPORT_STATUSES = ("pending", "running", "complete", "failed")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def _household_fk(*, primary_key: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(
        sa.Uuid,
        sa.ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=primary_key,
        nullable=False,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class Household(Base):
    """The tenant. One shared family login, one care recipient, no users table."""

    __tablename__ = "households"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)  # argon2 via pwdlib
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # A COLUMN, not a table: exactly one care recipient per household, locked by decision.
    care_recipient_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'Europe/London'"), default="Europe/London"
    )
    # THE REVOCATION MECHANISM. The signed cookie carries the version it was minted with, and
    # `require_household` 401s when it no longer matches this column. Bumping it is the only way
    # to invalidate a stolen or stranded 30-day cookie without rotating `SESSION_SECRET`, which
    # signs every household out of every device. Bumped by a password change and by
    # POST /api/household/sign-out-everywhere.
    #
    # Default 1, not 0, and NOT NULL: cookies minted before this column existed carry no version
    # at all and are read as version 1, so the deploy that adds it signs nobody out — and the
    # first bump revokes them along with everything else.
    session_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1"), default=1
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str) -> str:
        """A bad tz name is silent everywhere else: dedup buckets, day dividers, "yesterday".

        `ZoneInfo(bad)` only raises at render time, deep inside a request that has nothing to
        do with the write that caused it. Reject at the write instead.
        """
        if value not in available_timezones():
            raise ValueError(f"{value!r} is not a known timezone.")
        return value


class Member(Base):
    """A WhatsApp participant. NOT a login account — see `user_id`."""

    __tablename__ = "members"
    __table_args__ = (
        # Partial, because the common case is a member with only one of the two identifiers:
        # exports give a display name and no JID at all.
        sa.Index(
            "uq_members_household_wa_jid",
            "household_id",
            "wa_jid",
            unique=True,
            postgresql_where=sa.text("wa_jid IS NOT NULL"),
        ),
        sa.Index(
            "uq_members_household_wa_lid",
            "household_id",
            "wa_lid",
            unique=True,
            postgresql_where=sa.text("wa_lid IS NOT NULL"),
        ),
        # Export-name matching: the .txt importer only knows "Sarah", case as typed.
        sa.Index(
            "ix_members_household_lower_display_name",
            "household_id",
            sa.text("lower(display_name)"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    # HEDGE: unused in v1. No FK, no users table — deliberately. Carrying the column now makes
    # per-user accounts an additive change instead of a tenancy rewrite later.
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    display_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # BOTH identifiers, always. WhatsApp is migrating to @lid; GOWA emits both and `from` may be
    # a @lid when the phone number isn't resolvable. Keying on one alone silently fragments one
    # person into two members.
    wa_jid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # '4477...@s.whatsapp.net'
    wa_lid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # '2515...@lid'
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class WhatsappLink(Base):
    """chat_id -> household resolution. The tenant boundary for inbound webhook data."""

    __tablename__ = "whatsapp_links"
    __table_args__ = (sa.CheckConstraint("status IN ('pending', 'linked')", name="status"),)

    # PK, not just FK: one linked group per household in v1.
    household_id: Mapped[uuid.UUID] = _household_fk(primary_key=True)
    # A GOWA chat_id ('1203...@g.us') or the sentinel 'export:<household_id>'. Globally unique,
    # so no two households can claim the same group — that is what makes resolution safe.
    group_external_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'pending'"), default="pending"
    )
    linked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Message(Base):
    """Append-only. Edits, deletions and reactions are dropped at the adapter, by decision."""

    __tablename__ = "messages"
    __table_args__ = (
        sa.CheckConstraint(_in_check("provider", PROVIDERS), name="provider"),
        # Webhook replay guard: GOWA retries 5x with backoff on any non-200.
        sa.Index(
            "uq_messages_household_provider_message_id",
            "household_id",
            "provider_message_id",
            unique=True,
            postgresql_where=sa.text("provider_message_id IS NOT NULL"),
        ),
        # NOT scoped to provider, deliberately: re-uploading a longer export of the same chat
        # must not duplicate the overlap, and a live GOWA message must not duplicate the same
        # line already backfilled from an export.
        sa.Index("uq_messages_household_content_hash", "household_id", "content_hash", unique=True),
        # The extraction cursor. Partial, because the interesting set is always the small one.
        sa.Index(
            "ix_messages_household_sent_at_unextracted",
            "household_id",
            "sent_at",
            postgresql_where=sa.text("extracted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # NULL for .txt
    # sha256(household_id|sent_at|sender_key|text) — content identity, not row identity.
    content_hash: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    sender_wa_jid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sender_wa_lid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sender_display_name: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Line index within a .txt export. Export timestamps have no seconds, so this is the only
    # stable tiebreak when a dozen messages share a minute.
    source_ordinal: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    message_type: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'text'"), default="text"
    )
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # NULL for uncaptioned media
    # Verbatim provider payload. Re-extraction reads this, so never normalise it on the way in.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"), default=dict
    )
    # The ENTIRE incremental-extraction mechanism. NULL means "not yet fed to the LLM", which is
    # why a budget abort or a redeploy mid-run self-heals: the cron just picks the NULLs back up.
    extracted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = _created_at()


class LlmRun(Base):
    """One row per call attempt, including failures — a failed run is still the audit trail."""

    __tablename__ = "llm_runs"
    __table_args__ = (
        sa.Index(
            "ix_llm_runs_household_created_at",
            "household_id",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)  # extract | report | merge
    # ok | incomplete | filtered | error | budget_denied. No CHECK: the gateway owns this
    # vocabulary and adding a value must not need a migration.
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # sha256(instructions||input)[:32] — identifies a repeated call without storing the prompt.
    request_fingerprint: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    response_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1"), default=1
    )
    input_tokens: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    cached_input_tokens: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    output_tokens: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    # Reasoning tokens bill as output at $30/M and are invisible in output_tokens' effect on the
    # response, so a run's cost is unexplainable without them.
    reasoning_tokens: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    # The budget guard sums this over 30 days. Without the column, IMPORT_MAX_SPEND_USD and
    # LLM_MONTHLY_BUDGET_USD_PER_HOUSEHOLD are both uncomputable.
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 6), nullable=False, server_default=sa.text("0"), default=Decimal("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Event(Base):
    """The feed. Four kinds, one row per real-world happening for its whole life."""

    __tablename__ = "events"
    __table_args__ = (
        sa.CheckConstraint(_in_check("kind", EVENT_KINDS), name="kind"),
        sa.CheckConstraint(
            _in_check("occurred_at_precision", OCCURRED_AT_PRECISIONS),
            name="occurred_at_precision",
        ),
        # The merge guarantee: a re-extraction of the same happening lands on this row.
        sa.Index("uq_events_household_dedup_key", "household_id", "dedup_key", unique=True),
        # Exactly the feed query: newest first, deleted rows excluded.
        sa.Index(
            "ix_events_household_occurred_at_id",
            "household_id",
            sa.text("occurred_at DESC"),
            sa.text("id DESC"),
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # NOT NULL, falling back to the earliest source message's sent_at. Postgres DESC sorts
    # NULLS FIRST, so a nullable column would pin every undated event to the top of the feed.
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    occurred_at_precision: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'exact'"), default="exact"
    )
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Kind-specific fields, folded from the LLM's flat ExtractedEvent by the API layer.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"), default=dict
    )
    actor_member_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        sa.ARRAY(sa.Uuid), nullable=False, server_default=sa.text("'{}'::uuid[]"), default=list
    )
    # [{message_id, sent_at, sender, quote}] — replaces an event_sources join table, because
    # SourceDisclosure needs the verbatim quote inline and a join buys nothing at this scale.
    source_excerpts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    # [{at, source_message_ids, llm_run_id}] appended on every merge, so the UI can say
    # "mentioned 3x" and a wrong merge is inspectable rather than lossy.
    occurrences: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    # Columns a human has edited. Re-extraction never writes these back.
    user_edited_fields: Mapped[list[str]] = mapped_column(
        sa.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]"), default=list
    )
    # Set by Split, so a deliberately separated event is never re-merged by the next run.
    pinned: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false"), default=False
    )
    # "llm:<sha256>" or "human:<event_id>" (never merged). Unique per household, not globally.
    dedup_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True
    )
    # Non-null means a human touched it, and re-extraction must never overwrite it again.
    edited_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Soft delete, and the row keeps its dedup_key: a tombstone is what stops the next
    # extraction run resurrecting everything the family deleted.
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Import(Base):
    """One uploaded .txt export. `file_sha256` is what makes a re-upload a 409."""

    __tablename__ = "imports"
    __table_args__ = (
        sa.UniqueConstraint("household_id", "file_sha256", name="uq_imports_household_file_sha256"),
        sa.CheckConstraint(_in_check("status", IMPORT_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    file_sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    inserted_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0"), default=0
    )
    # Both are user-confirmed, never sniffed: dd/mm vs mm/dd is undecidable in an export
    # spanning under 12 days, and an export carries no offset at all. Stored so a later bug
    # report can be reproduced with the exact interpretation the family chose.
    dayfirst: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    timezone: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'pending'"), default="pending"
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Report(Base):
    """A generated care-management report. Citations resolve to events before storage."""

    __tablename__ = "reports"
    __table_args__ = (sa.CheckConstraint(_in_check("status", REPORT_STATUSES), name="status"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    household_id: Mapped[uuid.UUID] = _household_fk()
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'pending'"), default="pending"
    )
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    urgent_flag: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false"), default=False
    )
    urgent_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # [{heading, body_markdown, citations: [{handle, event_id, ...}]}]
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    questions_for_the_doctor: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    watch_items: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    data_gaps: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    period_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
