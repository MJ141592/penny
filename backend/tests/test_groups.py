"""The join-safety ledger, tested against real Postgres and a real race.

These are the two guards that stand between a deploy and a password in a stranger's group chat,
and both of them fail SILENTLY when broken. `observe_group` degrading into a
SELECT-then-INSERT still returns True — just to two callers instead of one, and only when two
deliveries land in the same few milliseconds, which is exactly what a busy group and five GOWA
retries produce and what a single-connection test never reproduces. So the concurrency test
here uses two real connections that can block on each other, like `test_onboarding.py` does.

The quiet-period tests are cheap and unglamorous and exist because the temptation to delete
that function is real: it makes a join in the first three minutes after a deploy do nothing,
which reads like a bug until you know it is the mitigation for whatsmeow re-emitting
JoinedGroup for pre-existing groups during app-state sync.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.groups
from app.db import to_asyncpg_url
from app.groups import (
    DEFAULT_STARTUP_QUIET_PERIOD_SECONDS,
    in_startup_quiet_period,
    is_known_group,
    observe_group,
    quiet_period_seconds,
)
from app.models import Base, KnownGroup


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """A real engine, because these tests need two connections that can block on each other."""
    engine = create_async_engine(to_asyncpg_url(db_url), pool_size=5)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


def _group_id() -> str:
    """A fresh chat id per test — the ledger is global and rows outlive a test by design."""
    return f"12036304{uuid.uuid4().hex[:12]}@g.us"


async def _observe(engine: AsyncEngine, group_external_id: str) -> bool:
    """One delivery: its own session and transaction, committed like `get_session` would."""
    async with AsyncSession(engine) as session:
        first = await observe_group(session, group_external_id)
        await session.commit()
        return first


async def _cleanup(engine: AsyncEngine, group_external_id: str) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(
            sa.delete(KnownGroup).where(KnownGroup.group_external_id == group_external_id)
        )
        await session.commit()


@pytest.mark.db
async def test_first_sighting_is_true_exactly_once(engine: AsyncEngine) -> None:
    """The whole contract, in the simple case: True once, False forever after."""
    group = _group_id()
    try:
        assert await _observe(engine, group) is True
        assert await _observe(engine, group) is False
        assert await _observe(engine, group) is False
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_concurrent_deliveries_see_exactly_one_first_sighting(engine: AsyncEngine) -> None:
    """Two deliveries in flight at once, ONE True, one row.

    This is the test that matters. GOWA retries five times with backoff and a busy group
    delivers in parallel, so the same group really does arrive twice at once. If this degrades
    to check-then-insert, both callers read "not there", both return True, and — with a caller
    that welcomes on a first sighting — the family gets two passwords. Nothing raises and no
    other test goes red.
    """
    group = _group_id()
    try:
        results = await asyncio.gather(_observe(engine, group), _observe(engine, group))
        assert sorted(results) == [False, True]

        async with AsyncSession(engine) as session:
            rows = await session.scalar(
                sa.select(sa.func.count())
                .select_from(KnownGroup)
                .where(KnownGroup.group_external_id == group)
            )
        assert rows == 1
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_ten_concurrent_deliveries_still_see_one(engine: AsyncEngine) -> None:
    """Two connections could pass by luck of scheduling; ten cannot."""
    group = _group_id()
    try:
        results = await asyncio.gather(*(_observe(engine, group) for _ in range(10)))
        assert results.count(True) == 1
        assert results.count(False) == 9
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_rollback_leaves_the_group_unrecorded(engine: AsyncEngine) -> None:
    """The truthful reading of True is "first time, IF this transaction commits".

    A handler that raises after observing must not leave a claim behind — the request did not
    happen, so the group has not been met. Safe direction: the next event is first again, which
    costs at most a duplicate welcome attempt that the onboarding lock already de-duplicates.
    """
    group = _group_id()
    try:
        async with AsyncSession(engine) as session:
            assert await observe_group(session, group) is True
            await session.rollback()

        async with AsyncSession(engine) as session:
            assert await is_known_group(session, group) is False
        assert await _observe(engine, group) is True
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_is_known_group_reads_without_recording(engine: AsyncEngine) -> None:
    """The read must never make its own answer true — that is why it is a separate function.

    A caller that only wants to ASK ("is this a group Penny has been sitting in for months?")
    would otherwise claim the group as a side effect, and the next genuine join would find it
    already known and stay silent forever.
    """
    group = _group_id()
    try:
        async with AsyncSession(engine) as session:
            assert await is_known_group(session, group) is False
            assert await is_known_group(session, group) is False
            await session.commit()

        # Still unrecorded, so a first sighting is still available.
        assert await _observe(engine, group) is True

        async with AsyncSession(engine) as session:
            assert await is_known_group(session, group) is True
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_groups_are_independent(engine: AsyncEngine) -> None:
    """Seeing one group must say nothing about another — the arbiter is the primary key."""
    first, second = _group_id(), _group_id()
    try:
        assert await _observe(engine, first) is True
        assert await _observe(engine, second) is True
        assert await _observe(engine, first) is False
    finally:
        await _cleanup(engine, first)
        await _cleanup(engine, second)


@pytest.mark.db
async def test_first_seen_at_is_set_and_never_moves(engine: AsyncEngine) -> None:
    """A repeat sighting is a no-op, not an UPDATE. The column is evidence, not a clock."""
    group = _group_id()
    try:
        assert await _observe(engine, group) is True
        async with AsyncSession(engine) as session:
            original = await session.scalar(
                sa.select(KnownGroup.first_seen_at).where(KnownGroup.group_external_id == group)
            )
        assert original is not None

        await asyncio.sleep(0.05)
        assert await _observe(engine, group) is False

        async with AsyncSession(engine) as session:
            after = await session.scalar(
                sa.select(KnownGroup.first_seen_at).where(KnownGroup.group_external_id == group)
            )
        assert after == original
    finally:
        await _cleanup(engine, group)


def test_quiet_period_is_on_for_a_young_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that started a moment ago does not believe a join event.

    This is the deploy case: whatsmeow reconnects, replays app-state, and re-emits JoinedGroup
    for every group it was ALREADY in. Believing those is how a password reached chats nobody
    meant to invite Penny to.
    """
    monkeypatch.setattr(app.groups, "_PROCESS_STARTED_AT", time.monotonic())
    assert in_startup_quiet_period() is True


def test_quiet_period_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """And it does end — this is a startup window, not a permanent mute."""
    monkeypatch.setattr(
        app.groups,
        "_PROCESS_STARTED_AT",
        time.monotonic() - (DEFAULT_STARTUP_QUIET_PERIOD_SECONDS + 1),
    )
    assert in_startup_quiet_period() is False


def test_quiet_period_default_is_three_minutes() -> None:
    """Named, so a change to the number is a visible diff rather than a config drift."""
    assert DEFAULT_STARTUP_QUIET_PERIOD_SECONDS == 180.0
    # Whatever Settings currently says, the resolved window must be a real, positive duration:
    # a missing or zero-valued setting resolving to "no quiet period" is the failure mode this
    # module exists to prevent, and it would look exactly like everything working.
    assert quiet_period_seconds() > 0


def test_quiet_period_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is the escape hatch — for tests, and for a deliberate "let joins through" incident
    setting on a freshly paired number that belongs to no other groups."""
    monkeypatch.setattr(app.groups, "quiet_period_seconds", lambda: 0.0)
    monkeypatch.setattr(app.groups, "_PROCESS_STARTED_AT", time.monotonic())
    assert in_startup_quiet_period() is False


def test_quiet_period_survives_a_missing_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Settings field is ever dropped or renamed, the answer is the default window.

    Never an AttributeError raised out of a webhook — that is a non-2xx, which is five GOWA
    retries — and never a silent zero, which would quietly reopen the incident. `object()`
    stands in for a Settings that has no such attribute at all.
    """
    monkeypatch.setattr(app.groups, "get_settings", lambda: object())
    assert quiet_period_seconds() == DEFAULT_STARTUP_QUIET_PERIOD_SECONDS
    monkeypatch.setattr(app.groups, "_PROCESS_STARTED_AT", time.monotonic())
    assert in_startup_quiet_period() is True
