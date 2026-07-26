"""Onboarding invariants that rot silently.

The one that matters is the concurrency test. GOWA retries five times with backoff and a busy
group delivers several messages before the first has finished, so every one of those deliveries
races to provision the same group. If the guard breaks, nothing fails and no test goes red — the
family simply receives five welcome messages carrying five different passwords, in the chat, on
the day they meet the product. That failure is only visible from two real connections against a
real Postgres, which is what these tests are.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.onboarding
from app.db import to_asyncpg_url
from app.models import Base, Household, WhatsappLink
from app.onboarding import Provisioned, generate_username, provision_for_group, welcome_message

if TYPE_CHECKING:
    from app.config import Settings

# Two words, NO trailing digits. The digits went because this string sits directly above a
# four-word password in the welcome message, and two hyphenated strings that differ only in
# whether one ends in digits is a login form filled in wrong.
USERNAME_SHAPE = re.compile(r"^[a-z]+-[a-z]+$")


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


async def _provision(engine: AsyncEngine, group_external_id: str) -> Provisioned | None:
    """One delivery: its own session, its own transaction, committed like `get_session` would."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await provision_for_group(session, group_external_id)
        await session.commit()
        return result


async def _cleanup(engine: AsyncEngine, group_external_id: str) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(
            sa.delete(Household).where(
                Household.id.in_(
                    sa.select(WhatsappLink.household_id).where(
                        WhatsappLink.group_external_id == group_external_id
                    )
                )
            )
        )
        await session.commit()


async def _households_for(engine: AsyncEngine, group_external_id: str) -> list[Household]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    sa.select(Household)
                    .join(WhatsappLink, WhatsappLink.household_id == Household.id)
                    .where(WhatsappLink.group_external_id == group_external_id)
                )
            ).scalars()
        )


@pytest.mark.db
async def test_concurrent_deliveries_provision_exactly_one_household(
    engine: AsyncEngine,
    settings_override: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two deliveries, two connections, one household and ONE welcome message."""
    settings_override(onboarding_enabled=True, onboarding_max_households=100)
    group = f"12036304{uuid.uuid4().hex[:12]}@g.us"
    try:
        with caplog.at_level("INFO", logger="app.onboarding"):
            first, second = await asyncio.gather(
                _provision(engine, group), _provision(engine, group)
            )
        events = [r.getMessage() for r in caplog.records]

        assert first is not None and second is not None
        # Exactly one winner. Whichever it was, the loser must be told to stay quiet.
        assert sorted([first.created, second.created]) == [False, True]
        winner = first if first.created else second
        loser = second if first.created else first
        assert loser.household_id == winner.household_id
        assert loser.passphrase == ""  # there is nothing to send, so there is nothing to leak
        assert winner.passphrase

        households = await _households_for(engine, group)
        assert len(households) == 1
        assert households[0].id == winner.household_id
        assert households[0].username == winner.username
        # The signal first-run setup keys off. Nobody has told us who is being cared for yet.
        assert households[0].care_recipient_name == "your family member"

        # NON-VACUITY. Both deliveries really were in flight at once, and the loser was stopped
        # by the LOCK — it re-checked and found the winner's committed row. Had the lock quietly
        # stopped serialising (a wrong key, a session-scoped lock released too early), the loser
        # would have reached the INSERT and come back through the index backstop as
        # `onboarding.lost_race` instead, still with one household and a green test.
        assert "onboarding.already_provisioned" in events
        assert "onboarding.lost_race" not in events
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_unique_index_still_wins_when_the_advisory_lock_is_bypassed(
    engine: AsyncEngine,
    settings_override: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is cooperative; the index is not. This is the backstop, exercised.

    Without disabling the lock this path is unreachable, so it would rot into an unhandled
    IntegrityError escaping a webhook that has to answer 200.
    """
    settings_override(onboarding_enabled=True, onboarding_max_households=100)

    async def _no_lock(session: AsyncSession, group_external_id: str) -> None:
        return None

    monkeypatch.setattr(app.onboarding, "_take_lock", _no_lock)
    group = f"12036304{uuid.uuid4().hex[:12]}@g.us"
    try:
        results = await asyncio.gather(_provision(engine, group), _provision(engine, group))
        assert sorted(r.created for r in results if r is not None) == [False, True]
        assert len(await _households_for(engine, group)) == 1
    finally:
        await _cleanup(engine, group)


@pytest.mark.db
async def test_cap_refuses_and_names_itself(
    engine: AsyncEngine,
    settings_override: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cap is the only thing standing between a leaked phone number and the OpenAI bill."""
    settings_override(onboarding_enabled=True, onboarding_max_households=0)
    group = f"12036304{uuid.uuid4().hex[:12]}@g.us"
    try:
        with caplog.at_level("WARNING"):
            assert await _provision(engine, group) is None
        assert await _households_for(engine, group) == []
        record = next(r for r in caplog.records if r.getMessage() == "onboarding.cap_reached")
        assert record.onboarding_max_households == 0
    finally:
        await _cleanup(engine, group)


async def test_disabled_never_touches_the_database(
    settings_override: Callable[..., Settings],
) -> None:
    """ONBOARDING_ENABLED=false restores the old silent 200. `None` for a session proves it
    short-circuits before any query — the switch is useless if it still costs a round trip."""
    settings_override(onboarding_enabled=False)
    assert await provision_for_group(cast(AsyncSession, None), "1203@g.us") is None


def test_username_is_readable_and_not_a_uuid() -> None:
    names = {generate_username() for _ in range(200)}
    assert all(USERNAME_SHAPE.match(name) for name in names)
    # Not a hard guarantee, just a smoke test that it is not returning a constant.
    assert len(names) > 150


def test_welcome_message_carries_the_credential_and_its_warning() -> None:
    text = welcome_message(
        "cedar-thistle-04", "amber-willow-flint-tundra-marble-gecko-71", "https://pennyai.chat/"
    )
    assert "https://pennyai.chat\n" in text  # trailing slash trimmed, not doubled
    assert "cedar-thistle-04" in text
    assert "amber-willow-flint-tundra-marble-gecko-71" in text
    # The two lines that exist for reasons that outlive the copy.
    assert "scroll back" in text
    assert "Settings" in text
    # WhatsApp has no markdown: asterisks would render literally, around a password.
    assert "*" not in text and "_" not in text
