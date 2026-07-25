"""Schema tests. Every one of these asserts a guarantee, not a column list.

The partial unique indexes ARE the idempotency story: a replayed GOWA webhook, a re-uploaded
longer export, and a second extraction run all rely on the database rejecting the duplicate.
That rejection is invisible in Python — it only exists in Postgres — so these tests need a
real Postgres and skip when `PENNY_TEST_DATABASE_URL` is unset.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.config
import app.db
from app.db import to_asyncpg_url
from app.models import Base, Event, Household, Import, Member, Message, WhatsappLink

NOW = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    """A session that never commits, so tests leave the database exactly as they found it.

    `create_all` is checkfirst-idempotent, so this coexists with an alembic-migrated database
    instead of dropping it — the models and the migration are proven identical by the
    empty-autogenerate check, so whichever built the tables, they are the same tables.
    """
    engine = create_async_engine(to_asyncpg_url(db_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine) as session:
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


async def make_household(session: AsyncSession, **kwargs: Any) -> Household:
    household = Household(
        username=f"fam-{uuid.uuid4().hex[:12]}",
        password_hash="$argon2id$not-a-real-hash",
        name="The Shaws",
        care_recipient_name="Margaret",
        **kwargs,
    )
    session.add(household)
    await session.flush()
    return household


def make_message(household: Household, text: str, **kwargs: Any) -> Message:
    kwargs.setdefault("provider", "whatsapp_export")
    kwargs.setdefault("content_hash", hashlib.sha256(text.encode()).digest())
    kwargs.setdefault("sent_at", NOW)
    return Message(household_id=household.id, text=text, **kwargs)


async def expect_conflict(session: AsyncSession, obj: Any) -> None:
    """Insert inside a SAVEPOINT so the failure doesn't poison the surrounding transaction."""
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(obj)
            await session.flush()


def test_timezone_is_validated_on_write() -> None:
    """No DB needed: a bad tz name only explodes at render time, far from the write."""
    with pytest.raises(ValueError, match="not a known timezone"):
        Household(
            username="x", password_hash="x", name="x", care_recipient_name="x", timezone="Mars/Base"
        )

    ok = Household(
        username="x", password_hash="x", name="x", care_recipient_name="x", timezone="Asia/Tokyo"
    )
    assert ok.timezone == "Asia/Tokyo"


@pytest.mark.db
async def test_wa_jid_is_unique_per_household(session: AsyncSession) -> None:
    household = await make_household(session)
    session.add(
        Member(household_id=household.id, display_name="Sarah", wa_jid="447@s.whatsapp.net")
    )
    await session.flush()

    await expect_conflict(
        session,
        Member(household_id=household.id, display_name="Sarah (dup)", wa_jid="447@s.whatsapp.net"),
    )

    # Same JID, different household: two families may both contain the same person.
    other = await make_household(session)
    session.add(Member(household_id=other.id, display_name="Sarah", wa_jid="447@s.whatsapp.net"))
    await session.flush()


@pytest.mark.db
async def test_wa_lid_is_unique_per_household(session: AsyncSession) -> None:
    """@lid gets its own index: keying on wa_jid alone fragments one person into two members."""
    household = await make_household(session)
    session.add(Member(household_id=household.id, display_name="Tom", wa_lid="2515@lid"))
    await session.flush()

    await expect_conflict(
        session, Member(household_id=household.id, display_name="Tom (dup)", wa_lid="2515@lid")
    )


@pytest.mark.db
async def test_members_without_identifiers_are_never_collapsed(session: AsyncSession) -> None:
    """The indexes are PARTIAL for this reason: a .txt export gives names and no JIDs at all."""
    household = await make_household(session)
    session.add_all(
        [
            Member(household_id=household.id, display_name="Sarah"),
            Member(household_id=household.id, display_name="Tom"),
            Member(household_id=household.id, display_name="Priya"),
        ]
    )
    await session.flush()

    count = await session.scalar(
        sa.select(sa.func.count()).select_from(Member).where(Member.household_id == household.id)
    )
    assert count == 3


@pytest.mark.db
async def test_replayed_provider_message_id_is_rejected(session: AsyncSession) -> None:
    """GOWA retries 5x with backoff; the webhook must be idempotent on payload.id."""
    household = await make_household(session)
    session.add(make_message(household, "hi", provider="gowa", provider_message_id="3EB0A1B2C3"))
    await session.flush()

    await expect_conflict(
        session,
        make_message(household, "hi (replay)", provider="gowa", provider_message_id="3EB0A1B2C3"),
    )


@pytest.mark.db
async def test_null_provider_message_ids_do_not_collide(session: AsyncSession) -> None:
    """Every .txt line has a NULL provider_message_id; a plain unique index would allow one."""
    household = await make_household(session)
    session.add_all([make_message(household, "morning all"), make_message(household, "night")])
    await session.flush()


@pytest.mark.db
async def test_content_hash_is_unique_across_providers(session: AsyncSession) -> None:
    """NOT scoped to provider, deliberately.

    The same message can arrive twice by two different routes: live from GOWA, and again in a
    re-uploaded, longer export of the same chat. Scoping the index to provider would let the
    overlap through and show the family every message twice.
    """
    household = await make_household(session)
    digest = hashlib.sha256(b"just got back from the GP").digest()
    session.add(make_message(household, "a", provider="gowa", content_hash=digest))
    await session.flush()

    await expect_conflict(
        session,
        make_message(household, "a", provider="whatsapp_export", content_hash=digest),
    )

    # Scoped to household, though: an identical sentence in another family is another message.
    other = await make_household(session)
    session.add(make_message(other, "a", provider="gowa", content_hash=digest))
    await session.flush()


@pytest.mark.db
async def test_provider_check_constraint(session: AsyncSession) -> None:
    household = await make_household(session)
    await expect_conflict(session, make_message(household, "x", provider="telegram"))


@pytest.mark.db
async def test_dedup_key_is_unique_per_household(session: AsyncSession) -> None:
    """The merge guarantee: re-extracting the same appointment lands on the same row."""
    household = await make_household(session)
    key = "llm:" + hashlib.sha256(b"appt|dr-aziz|2026-07-17").hexdigest()
    session.add(
        Event(
            household_id=household.id,
            kind="appointment",
            occurred_at=NOW,
            title="GP appointment, Dr Aziz",
            dedup_key=key,
        )
    )
    await session.flush()

    await expect_conflict(
        session,
        Event(
            household_id=household.id,
            kind="appointment",
            occurred_at=NOW + timedelta(hours=3),
            title="GP appointment (mentioned again)",
            dedup_key=key,
        ),
    )

    other = await make_household(session)
    session.add(
        Event(household_id=other.id, kind="appointment", occurred_at=NOW, title="GP", dedup_key=key)
    )
    await session.flush()


@pytest.mark.db
async def test_event_occurred_at_is_not_null(session: AsyncSession) -> None:
    """Postgres DESC sorts NULLS FIRST: one undated event would pin itself to the top of the feed."""
    household = await make_household(session)
    await expect_conflict(
        session,
        Event(household_id=household.id, kind="note", title="undated", dedup_key="note:undated"),
    )


@pytest.mark.db
@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "reminder"), ("occurred_at_precision", "decade")],
)
async def test_event_enum_checks(session: AsyncSession, column: str, value: str) -> None:
    household = await make_household(session)
    fields: dict[str, Any] = {
        "household_id": household.id,
        "kind": "note",
        "occurred_at": NOW,
        "title": "t",
        "dedup_key": f"note:{column}",
        column: value,
    }
    await expect_conflict(session, Event(**fields))


@pytest.mark.db
async def test_one_group_links_to_one_household(session: AsyncSession) -> None:
    """group_external_id is globally unique: two households claiming one chat is a tenant leak."""
    first = await make_household(session)
    second = await make_household(session)
    session.add(WhatsappLink(household_id=first.id, group_external_id="1203@g.us"))
    await session.flush()

    await expect_conflict(
        session, WhatsappLink(household_id=second.id, group_external_id="1203@g.us")
    )


@pytest.mark.db
async def test_reimporting_the_same_file_is_rejected(session: AsyncSession) -> None:
    household = await make_household(session)
    row = {
        "household_id": household.id,
        "file_sha256": "3b1f0c",
        "filename": "WhatsApp Chat with Mum's Care.txt",
        "dayfirst": True,
        "timezone": "Europe/London",
    }
    session.add(Import(**row))
    await session.flush()

    await expect_conflict(session, Import(**row))


@pytest.mark.db
async def test_deleting_a_household_takes_everything_with_it(session: AsyncSession) -> None:
    """DELETE /api/household is one statement; anything not cascaded is orphaned health data."""
    household = await make_household(session)
    member = Member(household_id=household.id, display_name="Sarah")
    session.add(member)
    await session.flush()
    session.add_all(
        [
            make_message(household, "morning", member_id=member.id),
            Event(
                household_id=household.id,
                kind="note",
                occurred_at=NOW,
                title="Bins out",
                actor_member_id=member.id,
                dedup_key="note:bins",
            ),
            Import(
                household_id=household.id,
                file_sha256="abc",
                filename="chat.txt",
                dayfirst=True,
                timezone="Europe/London",
            ),
            WhatsappLink(household_id=household.id, group_external_id=f"g-{household.id}@g.us"),
        ]
    )
    await session.flush()

    await session.execute(sa.delete(Household).where(Household.id == household.id))

    for model in (Member, Message, Event, Import, WhatsappLink):
        remaining = await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.household_id == household.id)
        )
        assert remaining == 0, f"{model.__tablename__} survived its household"


@pytest.mark.db
async def test_get_session_owns_the_transaction_boundary(
    db_url: str, monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """Implementation rule #1: the dependency commits, handlers never do.

    If someone "simplifies" the try/except away, every write in the app silently disappears at
    request end and every test that uses its own session still passes. Hence this one.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    app.config.get_settings.cache_clear()
    await app.db.dispose_engine()
    kept = f"boundary-kept-{uuid.uuid4().hex[:8]}"
    doomed = f"boundary-doomed-{uuid.uuid4().hex[:8]}"

    def household(username: str) -> Household:
        return Household(
            username=username,
            password_hash="x",
            name="The Shaws",
            care_recipient_name="Margaret",
        )

    try:
        # Driving the generator by hand is exactly what FastAPI does with a yield dependency:
        # exhaust it on success, `athrow` the handler's exception into it on failure.
        gen = app.db.get_session()
        handler_session = await anext(gen)
        handler_session.add(household(kept))
        with pytest.raises(StopAsyncIteration):
            await anext(gen)  # no commit in "the handler", yet the row must be durable
        assert await session.scalar(sa.select(Household.id).where(Household.username == kept))

        gen = app.db.get_session()
        handler_session = await anext(gen)
        handler_session.add(household(doomed))
        with pytest.raises(RuntimeError, match="handler blew up"):
            await gen.athrow(RuntimeError("handler blew up"))
        assert not await session.scalar(sa.select(Household.id).where(Household.username == doomed))
    finally:
        gen = app.db.get_session()
        cleanup = await anext(gen)
        await cleanup.execute(sa.delete(Household).where(Household.username.in_([kept, doomed])))
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await app.db.dispose_engine()
        app.config.get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.parametrize(
    "index_name",
    [
        "ix_members_household_lower_display_name",
        "ix_messages_household_sent_at_unextracted",
        "ix_events_household_occurred_at_id",
    ],
)
async def test_non_unique_indexes_exist(session: AsyncSession, index_name: str) -> None:
    """These three are invisible to behaviour, so nothing else would notice them going missing.

    The expression index in particular (`lower(display_name)`) is the one autogenerate is most
    likely to mishandle.
    """
    found = await session.scalar(
        sa.select(sa.text("indexdef"))
        .select_from(sa.text("pg_indexes"))
        .where(sa.text("indexname = :name"))
        .params(name=index_name)
    )
    assert found, f"{index_name} is missing"
