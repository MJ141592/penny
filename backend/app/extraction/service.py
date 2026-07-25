"""The one entry point that runs extraction for a household. Import flow AND webhook.

`extract_messages` is pure and knows nothing about a database; `persist_extraction` writes rows
and knows nothing about how to get them. This is the join: read the cursor, build the brief,
call the runner, persist the result. Both callers — the `.txt` import's `BackgroundTasks` job
and the GOWA webhook's post-200 work — go through here and nowhere else.

WHY THE ADVISORY LOCK IS NOT OPTIONAL

A busy household WILL race. A family uploads a six-month export, and while the background job
is chewing through it someone sends a message to the linked WhatsApp group; the webhook wants
to extract too. Both would read the same `extracted_at IS NULL` set, both would send it to the
model, and the family would be billed twice for one answer and shown two of every card until
the dedup key happened to collapse them.

`pg_try_advisory_xact_lock` is the cheapest correct answer: no table, no row, no cleanup, and
it is released by the transaction ending — including by the process dying, which a lock row in
a table is not. TRY, not the blocking form: a second run has nothing to add, so it returns
`ran=False` immediately rather than holding a connection open waiting for its turn.

THE SPEND CEILING ABORTS MID-FLIGHT, ON PURPOSE. The runner stops adding chunk ids to
`extracted_message_ids`, those messages keep `extracted_at IS NULL`, and the next run picks up
exactly where this one stopped. Resumability is a consequence of the cursor, not a mechanism.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.config import get_settings
from app.extraction.chunker import OVERLAP_MESSAGES, ChunkMessage
from app.extraction.persist import persist_extraction
from app.extraction.runner import OPEN_EVENTS_LIMIT, OpenEvent, extract_messages
from app.llm.gateway import BudgetExceededError, LLMGateway
from app.llm.recorder import DbRunRecorder
from app.models import Event, Household, LlmRun, Member, Message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Namespaced so this household's extraction lock can never collide with some other advisory
# lock we add later for the same household (report generation, member merge).
_LOCK_NAMESPACE = b"penny.extraction:"

# The window `llm_monthly_budget_usd_per_household` is measured over. A rolling 30 days, not
# a calendar month: a family that imports on the 31st should not get a fresh allowance on the
# 1st, and "you have spent $14 this month" that resets at midnight is not a guard.
BUDGET_WINDOW = timedelta(days=30)

# `<open_events>` is built from appointments that have not reported an outcome yet, because
# the whole point of the block is letting the model attach "it went fine" to something it is
# not currently reading. A 400-day floor keeps a long-dead appointment out of every prompt.
OPEN_EVENT_MAX_AGE = timedelta(days=400)


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What one call did. `ran=False` means another run held the lock — not a failure."""

    household_id: UUID
    ran: bool
    messages_considered: int = 0
    events_inserted: int = 0
    events_updated: int = 0
    events_protected: int = 0
    messages_stamped: int = 0
    # Messages this run considered and did NOT get through: a chunk whose call errored or
    # whose response would not parse, plus everything after a spend abort. They keep
    # `extracted_at IS NULL`, so the next run retries them — but the CALLER has to be told,
    # or it reports a run in which every single chunk failed as a completed one.
    messages_unextracted: int = 0
    chunks: int = 0
    cost_usd: Decimal = Decimal("0")
    aborted_reason: str | None = None


async def run_extraction_for_household(
    session: AsyncSession,
    household_id: UUID,
    *,
    max_spend_usd: Decimal | None = None,
) -> RunSummary:
    """Extract everything not yet extracted for one household. Never commits — see `app.db`."""
    if not await _acquire(session, household_id):
        log.info("extraction already running household=%s", household_id)
        return RunSummary(household_id=household_id, ran=False)

    household = await session.get(Household, household_id)
    if household is None:
        return RunSummary(household_id=household_id, ran=False)

    inert = await _stamp_inert(session, household_id)
    pending = await _unextracted(session, household_id)
    if not pending:
        return RunSummary(household_id=household_id, ran=True, messages_stamped=inert)

    tz = ZoneInfo(household.timezone)
    recorder = DbRunRecorder(session, household_id)
    gateway = LLMGateway(recorder=recorder, budget=_HouseholdBudget(session))

    result = await extract_messages(
        pending,
        care_brief=await build_care_brief(session, household),
        tz=tz,
        gateway=gateway,
        open_events=await _open_events(session, household_id, tz),
        max_spend_usd=max_spend_usd,
        household_id=household_id,
        already_extracted=await _context_before(session, household_id, pending[0].sent_at),
    )
    persisted = await persist_extraction(
        session,
        household_id,
        result,
        extraction_run_id=recorder.run_ids[-1] if recorder.run_ids else None,
    )
    return RunSummary(
        household_id=household_id,
        ran=True,
        messages_considered=len(pending),
        events_inserted=persisted.inserted,
        events_updated=persisted.updated,
        events_protected=persisted.protected,
        messages_stamped=persisted.stamped + inert,
        messages_unextracted=len(result.unextracted_message_ids),
        chunks=result.chunk_count,
        cost_usd=result.total_cost_usd,
        aborted_reason=result.aborted_reason,
    )


async def build_care_brief(session: AsyncSession, household: Household) -> str:
    """The block that turns "she" into a name.

    Two facts, both of which the database actually knows: who this household is caring for,
    and who talks in the chat. Naming the care recipient is what stops the model extracting
    Priya's dentist appointment and Alfie's temperature as care events — the fixture's five
    planted distractors are all "a symptom, but not hers".
    """
    names = (
        (
            await session.execute(
                sa.select(Member.display_name)
                .where(Member.household_id == household.id)
                .order_by(Member.display_name)
            )
        )
        .scalars()
        .all()
    )
    people = ", ".join(name for name in names if name) or "unknown"
    return (
        f"Care recipient: {household.care_recipient_name}. Messages about anyone else are not "
        "care events unless that person is the actor.\n"
        f"People in this chat: {people}.\n"
        f"Household timezone: {household.timezone}."
    )


# --- the lock -------------------------------------------------------------------------


def lock_key(household_id: UUID) -> int:
    """A stable signed 64-bit key. `hash()` would NOT do: it is salted per process."""
    digest = hashlib.blake2b(_LOCK_NAMESPACE + household_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _acquire(session: AsyncSession, household_id: UUID) -> bool:
    """Transaction-scoped, so it is released by commit, rollback, crash or connection loss."""
    return bool(
        (
            await session.execute(
                sa.select(sa.func.pg_try_advisory_xact_lock(lock_key(household_id)))
            )
        ).scalar_one()
    )


# --- reads ----------------------------------------------------------------------------


async def _stamp_inert(session: AsyncSession, household_id: UUID) -> int:
    """Stamp system lines as extracted without sending them anywhere. They cost nothing.

    Skipping them at the chunker and leaving `extracted_at` NULL looks harmless and is not:
    the import progress bar is `extracted_count / inserted_count`, so an import of a 2,000-line
    export containing 9 join notices would sit at 99% forever and the UI would never stop
    polling. Stamped here, deliberately and visibly, rather than quietly excluded everywhere.
    """
    result = await session.execute(
        sa.update(Message)
        .where(
            Message.household_id == household_id,
            Message.extracted_at.is_(None),
            Message.message_type == "system",
        )
        .values(extracted_at=datetime.now(UTC))
    )
    return result.rowcount or 0


async def _unextracted(session: AsyncSession, household_id: UUID) -> list[ChunkMessage]:
    """The cursor. System lines are excluded here rather than dropped later.

    A join notice or the encryption banner is a group event, not conversation, and paying a
    model to read it finds nothing — but it must still get `extracted_at` stamped, or the
    "unextracted" count never reaches zero and every subsequent run rebuilds the same chunk.
    That stamping happens in `_stamp_inert`, below, not by pretending they were extracted.
    """
    rows = (
        await session.execute(
            sa.select(
                Message.id,
                Message.sent_at,
                Message.sender_display_name,
                Message.text,
                Message.message_type,
                Message.payload,
                Message.source_ordinal,
            )
            .where(
                Message.household_id == household_id,
                Message.extracted_at.is_(None),
                Message.message_type != "system",
            )
            .order_by(Message.sent_at, Message.source_ordinal, Message.id)
        )
    ).all()
    return [
        ChunkMessage(
            id=row.id,
            sent_at=row.sent_at,
            sender_display_name=row.sender_display_name,
            text=row.text,
            message_type=row.message_type,
            media_filename=(row.payload or {}).get("filename"),
            source_ordinal=row.source_ordinal,
        )
        for row in rows
    ]


async def _context_before(
    session: AsyncSession, household_id: UUID, first_sent_at: datetime
) -> list[ChunkMessage]:
    """The tail of what was already extracted, as context-only for the first chunk.

    Without it, an incremental run starting mid-thread reads "she's still dizzy" with no
    antecedent, and either invents an event or drops a real one. These messages are context,
    never sources — the chunker forbids citing them, so they cannot be double-billed as
    evidence.
    """
    rows = (
        await session.execute(
            sa.select(
                Message.id,
                Message.sent_at,
                Message.sender_display_name,
                Message.text,
                Message.message_type,
                Message.source_ordinal,
            )
            .where(
                Message.household_id == household_id,
                Message.extracted_at.isnot(None),
                Message.message_type != "system",
                Message.sent_at <= first_sent_at,
            )
            .order_by(Message.sent_at.desc(), Message.source_ordinal.desc())
            .limit(OVERLAP_MESSAGES)
        )
    ).all()
    return [
        ChunkMessage(
            id=row.id,
            sent_at=row.sent_at,
            sender_display_name=row.sender_display_name,
            text=row.text,
            message_type=row.message_type,
            source_ordinal=row.source_ordinal,
        )
        for row in reversed(rows)
    ]


async def _open_events(session: AsyncSession, household_id: UUID, tz: ZoneInfo) -> list[OpenEvent]:
    """Appointments still awaiting an outcome, rendered for the `<open_events>` block.

    NO UUIDs and no dedup keys go in here: models transpose long hex runs, so open events are
    labelled `[E1]`… by the renderer and the model never sees an identifier it could get wrong.
    """
    cutoff = datetime.now(UTC) - OPEN_EVENT_MAX_AGE
    rows = (
        await session.execute(
            sa.select(Event.kind, Event.occurred_at, Event.title, Event.details)
            .where(
                Event.household_id == household_id,
                Event.deleted_at.is_(None),
                Event.kind == "appointment",
                Event.occurred_at >= cutoff,
                Event.details["status"].astext != "attended",
            )
            .order_by(Event.occurred_at.desc())
            .limit(OPEN_EVENTS_LIMIT)
        )
    ).all()
    return [
        OpenEvent(
            kind=row.kind,
            occurred_at=row.occurred_at.astimezone(tz).strftime("%Y-%m-%d"),
            title=row.title,
            detail=(row.details or {}).get("provider_name"),
        )
        for row in rows
    ]


# --- the monthly spend guard ----------------------------------------------------------


class _HouseholdBudget:
    """`BudgetGuard` over `llm_runs.cost_usd`. Checked ONCE per run, by the gateway.

    Reads the same session the run is writing into, so a run that has already overspent
    inside this transaction is refused on its next check rather than after it commits.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check(self, household_id: UUID | None) -> None:
        if household_id is None:
            return
        limit = get_settings().llm_monthly_budget_usd_per_household
        spent = (
            await self._session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(LlmRun.cost_usd), 0)).where(
                    LlmRun.household_id == household_id,
                    LlmRun.created_at >= datetime.now(UTC) - BUDGET_WINDOW,
                )
            )
        ).scalar_one()
        if Decimal(spent) >= limit:
            # This sentence reaches the family as a 409 body, so it is written for them.
            raise BudgetExceededError(
                f"This household has used its ${limit} monthly AI allowance. "
                "It resets as older activity ages out."
            )
