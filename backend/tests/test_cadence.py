"""The cadence gate: how many model calls a conversation is allowed to cost.

This file exists because of a measured production overrun, not a hypothetical one. Extraction
fired once per inbound message — 13 runs for 14 messages — and each run carries ~1,400 tokens
of fixed prompt overhead, so the bill came out at $0.00763/message against a planned $0.00042.
18x. At 120 messages/day a family would spend $27/month and trip a $15 budget on day 16.

What is asserted here is therefore a COST invariant, and the only honest way to state it is in
model calls: `_SpyRunner.calls` is the number of times money would have been spent. A test that
asserted on events or on `extracted_at` alone would still pass if every message got its own
call, which is exactly the bug.

The DB tests need real Postgres: the gate reads through the `extracted_at IS NULL` cursor and
holds a `pg_try_advisory_xact_lock`, neither of which SQLite has.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.extraction.service as service
from app.db import to_asyncpg_url
from app.extraction.chunker import ChunkMessage
from app.extraction.runner import ExtractionRunResult
from app.extraction.service import (
    cadence_decision,
    households_due_for_extraction,
    run_extraction_for_household,
)
from app.models import Base, Household, LlmRun, Message

if TYPE_CHECKING:
    from app.config import Settings

NOW = datetime.now(UTC)


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    """Never commits, so a run of this file leaves the database as it found it."""
    engine = create_async_engine(to_asyncpg_url(db_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine) as db:
            yield db
            await db.rollback()
    finally:
        await engine.dispose()


class _SpyRunner:
    """Stands in for `extract_messages`. Counts CALLS, because calls are what cost money."""

    def __init__(self) -> None:
        self.calls: list[list[ChunkMessage]] = []

    async def __call__(self, pending: list[ChunkMessage], **kwargs: Any) -> ExtractionRunResult:
        self.calls.append(list(pending))
        # Report everything as extracted so `persist_extraction` stamps it, which is what a
        # real successful run does and what makes the "second run has nothing to do" case real.
        return ExtractionRunResult(extracted_message_ids=[m.id for m in pending])

    @property
    def batch_sizes(self) -> list[int]:
        return [len(c) for c in self.calls]


@pytest.fixture
def cadence(settings_override: Callable[..., Settings]) -> Callable[..., Settings]:
    """Pin the thresholds to the production defaults, explicitly, so a later default change
    cannot quietly turn these assertions into a different test.

    `openai_api_key` is set because `LLMGateway` is constructed before the runner is called
    and refuses to build without one. No request is ever made: `extract_messages` is a spy,
    so the transport underneath is never reached.
    """

    def _apply(**kwargs: object) -> Settings:
        return settings_override(
            openai_api_key="sk-test-not-a-real-key",
            extract_min_unextracted=40,
            extract_max_age_hours=6,
            **kwargs,
        )

    return _apply


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _SpyRunner:
    """No real model call ever happens in this file; a call here is a counted event."""
    spy = _SpyRunner()
    monkeypatch.setattr(service, "extract_messages", spy)
    return spy


async def make_household(session: AsyncSession) -> Household:
    household = Household(
        username=f"cadence-{uuid.uuid4().hex[:12]}",
        password_hash="$argon2id$not-a-real-hash",
        name="The Shaws",
        care_recipient_name="Margaret",
    )
    session.add(household)
    await session.flush()
    return household


async def ingest(
    session: AsyncSession,
    household: Household,
    count: int,
    *,
    minutes_ago: float = 0.0,
    message_type: str = "text",
) -> list[Message]:
    """`count` unextracted messages, oldest first, ending `minutes_ago` before now."""
    messages = []
    for i in range(count):
        text = f"{household.id}-{uuid.uuid4().hex}"
        messages.append(
            Message(
                household_id=household.id,
                provider="gowa",
                provider_message_id=uuid.uuid4().hex,
                content_hash=hashlib.sha256(text.encode()).digest(),
                sender_display_name="Sarah",
                sent_at=NOW - timedelta(minutes=minutes_ago) - timedelta(seconds=count - i),
                message_type=message_type,
                text=text,
            )
        )
    session.add_all(messages)
    await session.flush()
    return messages


async def unextracted_count(session: AsyncSession, household: Household) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.household_id == household.id, Message.extracted_at.is_(None))
        )
    ).scalar_one()


async def llm_run_count(session: AsyncSession, household: Household) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(LlmRun)
            .where(LlmRun.household_id == household.id)
        )
    ).scalar_one()


def chunk(minutes_ago: float) -> ChunkMessage:
    return ChunkMessage(
        id=uuid.uuid4(),
        sent_at=NOW - timedelta(minutes=minutes_ago),
        sender_display_name="Sarah",
        text="…",
    )


# --- the decision, without a database -------------------------------------------------


def test_count_trigger_is_inclusive_at_the_threshold(
    cadence: Callable[..., Settings],
) -> None:
    """39 waits, 40 goes. Off by one here is a 2.5% swing in the bill, silently."""
    cadence()
    assert cadence_decision([chunk(1)] * 39, now=NOW).extract is False
    decision = cadence_decision([chunk(1)] * 40, now=NOW)
    assert (decision.extract, decision.reason, decision.pending) == (True, "count", 40)


def test_age_trigger_is_strict_and_reads_the_oldest_message(
    cadence: Callable[..., Settings],
) -> None:
    """Exactly six hours is not yet six hours old. The list is oldest-first, so [0] decides."""
    cadence()
    assert cadence_decision([chunk(360), chunk(0)], now=NOW).extract is False
    decision = cadence_decision([chunk(361), chunk(0)], now=NOW)
    assert (decision.extract, decision.reason) == (True, "age")


def test_force_beats_both_triggers(cadence: Callable[..., Settings]) -> None:
    cadence()
    decision = cadence_decision([chunk(1)], force=True, now=NOW)
    assert (decision.extract, decision.reason) == (True, "forced")


def test_a_message_from_the_future_does_not_trip_the_age_trigger(
    cadence: Callable[..., Settings],
) -> None:
    """Phone clock skew must not become a spend trigger. Negative age is simply not > 6h."""
    cadence()
    assert cadence_decision([chunk(-600)], now=NOW).extract is False


# --- the gate, against real Postgres --------------------------------------------------


@pytest.mark.db
async def test_five_messages_cost_nothing(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """The regression itself: five inbound messages used to be five paid runs."""
    cadence()
    household = await make_household(session)
    await ingest(session, household, 5)

    summaries = [await run_extraction_for_household(session, household.id) for _ in range(5)]

    assert runner.calls == []
    assert await llm_run_count(session, household) == 0
    assert all(s.deferred and not s.ran for s in summaries)
    assert [s.messages_pending for s in summaries] == [5, 5, 5, 5, 5]
    assert await unextracted_count(session, household) == 5


@pytest.mark.db
async def test_crossing_the_threshold_runs_once_over_the_whole_backlog(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """40 messages, arriving one at a time, buy exactly ONE call covering all 40."""
    cadence()
    household = await make_household(session)

    for _ in range(39):
        await ingest(session, household, 1)
        await run_extraction_for_household(session, household.id)
    assert runner.calls == []

    await ingest(session, household, 1)
    summary = await run_extraction_for_household(session, household.id)

    assert runner.batch_sizes == [40]
    assert (summary.ran, summary.deferred) == (True, False)
    assert (summary.messages_pending, summary.messages_considered) == (40, 40)
    assert await unextracted_count(session, household) == 0

    # And the run after it is free again: the backlog is empty, then it rebuilds.
    await ingest(session, household, 3)
    assert (await run_extraction_for_household(session, household.id)).deferred is True
    assert runner.batch_sizes == [40]


@pytest.mark.db
async def test_the_age_trigger_flushes_a_short_stale_conversation(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """Seven messages about tomorrow's appointment must not wait for thirty-three more."""
    cadence()
    household = await make_household(session)
    await ingest(session, household, 7, minutes_ago=7 * 60)

    summary = await run_extraction_for_household(session, household.id)

    assert runner.batch_sizes == [7]
    assert (summary.ran, summary.deferred, summary.messages_considered) == (True, False, 7)
    assert await unextracted_count(session, household) == 0


@pytest.mark.db
async def test_force_extracts_immediately_for_the_import_flow(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """A family watching an upload progress bar is not waiting six hours for three messages."""
    cadence()
    household = await make_household(session)
    await ingest(session, household, 3)

    deferred = await run_extraction_for_household(session, household.id)
    forced = await run_extraction_for_household(session, household.id, force=True)

    assert deferred.deferred is True
    assert runner.batch_sizes == [3]
    assert (forced.ran, forced.deferred) == (True, False)
    assert await unextracted_count(session, household) == 0


@pytest.mark.db
async def test_system_lines_are_stamped_even_while_the_gate_defers(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """Otherwise an import progress bar of `extracted/inserted` never reaches 100%.

    System lines cost nothing to stamp — no model sees them — so holding them behind a gate
    whose whole purpose is to avoid model calls would be pure downside.
    """
    cadence()
    household = await make_household(session)
    await ingest(session, household, 3)
    await ingest(session, household, 4, message_type="system")

    summary = await run_extraction_for_household(session, household.id)

    assert runner.calls == []
    assert (summary.deferred, summary.messages_stamped) == (True, 4)
    # The four system lines are done; only the three real messages are still waiting.
    assert await unextracted_count(session, household) == 3


@pytest.mark.db
async def test_system_lines_cannot_push_a_household_over_the_threshold(
    session: AsyncSession, runner: _SpyRunner, cadence: Callable[..., Settings]
) -> None:
    """A group with 39 messages and 20 join notices has 39 messages' worth of value."""
    cadence()
    household = await make_household(session)
    await ingest(session, household, 39)
    await ingest(session, household, 20, message_type="system")

    summary = await run_extraction_for_household(session, household.id)

    assert runner.calls == []
    assert (summary.deferred, summary.messages_pending) == (True, 39)


# --- the work list the hourly tick is meant to run ------------------------------------


@pytest.mark.db
async def test_households_due_for_extraction_matches_the_gate(
    session: AsyncSession, cadence: Callable[..., Settings]
) -> None:
    """The tick's query and the per-call gate must agree, or the tick wakes households up to
    do nothing (and pays for the wake-up) or leaves the quiet ones stranded forever.

    NOTE: nothing calls this in production yet — `POST /api/internal/tick` is specified in
    `docs/api-contract.md` and unimplemented. Until it exists, a group that goes quiet below
    the count threshold waits for its next message rather than for the age trigger.
    """
    cadence()
    over_count = await make_household(session)
    stale = await make_household(session)
    quiet_but_fresh = await make_household(session)
    only_system = await make_household(session)

    await ingest(session, over_count, 40)
    await ingest(session, stale, 2, minutes_ago=7 * 60)
    await ingest(session, quiet_but_fresh, 5)
    await ingest(session, only_system, 60, minutes_ago=48 * 60, message_type="system")

    due = set(await households_due_for_extraction(session))

    assert over_count.id in due
    assert stale.id in due
    assert quiet_but_fresh.id not in due
    assert only_system.id not in due

    # Every household the tick would pick up actually extracts when it gets there.
    for household_id in (over_count.id, stale.id):
        pending = await service._unextracted(session, household_id)
        assert cadence_decision(pending).extract is True
