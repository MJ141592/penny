"""Penny is added to a group; a household appears and the credentials go back into the group.

These run against REAL Postgres and skip without `PENNY_TEST_DATABASE_URL`, because the two
things worth proving here only exist in the database. The first is that the whole path works at
all — signature, provisioning, the link row, the message that introduced Penny, and a welcome
message carrying credentials that genuinely open the account. The second is idempotency, and it
is the one that would rot silently: GOWA retries five times with exponential backoff on any
non-2xx, so the difference between "one household" and "five households, five passwords, five
welcome messages in the family's chat on day one" is invisible in a single-delivery test and
catastrophic in production.

The sidecar is a stub (`gowa.send_message` is monkeypatched) and so is extraction, so nothing
here makes a network call. Everything else is the real handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db
from app import gowa
from app.config import Settings
from app.db import to_asyncpg_url
from app.extraction.service import RunSummary
from app.main import app as penny_app
from app.models import Household, Message, WhatsappLink
from app.onboarding import welcome_message
from app.routers import webhooks

SECRET = "a-real-webhook-secret-not-the-published-default"
PUBLIC_URL = "https://pennyai.chat"

pytestmark = pytest.mark.db


# --- plumbing ---------------------------------------------------------------------------


def query(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """Run one read on its OWN engine, in its own loop.

    The app's engine lives in the TestClient's portal thread and its asyncpg connections are
    bound to that loop; borrowing it from the test thread is how you get "attached to a
    different loop" at random. A throwaway engine per assertion is cheap and cannot do that.
    """

    async def run() -> Any:
        engine = create_async_engine(to_asyncpg_url(db_url))
        try:
            async with AsyncSession(engine) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def delete_group(db_url: str, chat_id: str) -> None:
    """Remove whatever a test provisioned. ON DELETE CASCADE takes the messages and links."""

    async def run(session: AsyncSession) -> None:
        await session.execute(
            sa.delete(Household).where(
                Household.id.in_(
                    sa.select(WhatsappLink.household_id).where(
                        WhatsappLink.group_external_id == chat_id
                    )
                )
            )
        )
        await session.commit()

    query(db_url, run)


def households_for(db_url: str, chat_id: str) -> list[UUID]:
    return query(
        db_url,
        lambda s: s.scalars(
            sa.select(WhatsappLink.household_id).where(WhatsappLink.group_external_id == chat_id)
        ),
    ).all()


def messages_for(db_url: str, household_id: UUID) -> list[Message]:
    return query(
        db_url,
        lambda s: s.scalars(sa.select(Message).where(Message.household_id == household_id)),
    ).all()


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    """The welcome message is sent from a task detached from the request. Poll for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def payload(chat_id: str, message_id: str, text: str) -> dict[str, Any]:
    return {
        "event": "message",
        "device_id": "447473209317@s.whatsapp.net",
        "payload": {
            "id": message_id,
            "chat_id": chat_id,
            "timestamp": "2026-07-25T10:30:00Z",
            "is_from_me": False,
            "from": "447700900123@s.whatsapp.net",
            "from_lid": "251556368777322@lid",
            "from_name": "Sarah",
            "body": text,
        },
    }


def post_webhook(client: TestClient, body: dict[str, Any], secret: str = SECRET) -> Any:
    """Sign the EXACT bytes we send — that is what the handler verifies."""
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/whatsapp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


@pytest.fixture
def chat_id() -> str:
    """A group id of this test's own, so parallel runs and leftovers cannot collide."""
    return f"120363{uuid4().hex[:14]}@g.us"


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The stub sidecar. `webhooks` calls `gowa.send_message`, so patch it on the module."""
    sent: list[tuple[str, str]] = []

    async def fake_send(chat: str, text: str) -> gowa.GowaSendResult:
        sent.append((chat, text))
        return gowa.GowaSendResult(ok=True, message_id=f"stub-{len(sent)}")

    monkeypatch.setattr(gowa, "send_message", fake_send)
    return sent


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extraction still gets scheduled and still runs; it just never reaches OpenAI."""

    async def fake_extraction(session: AsyncSession, household_id: UUID, **_: Any) -> RunSummary:
        return RunSummary(household_id=household_id, ran=False)

    monkeypatch.setattr(webhooks, "run_extraction_for_household", fake_extraction)


@pytest.fixture
def make_client(
    db_url: str,
    settings_override: Callable[..., Settings],
    chat_id: str,
    sends: list[tuple[str, str]],
    no_llm: None,
) -> Iterator[Callable[..., TestClient]]:
    """A TestClient wired to the real database, with onboarding on unless a test says otherwise.

    `with TestClient(...)` matters: outside the context manager every request gets its own
    event loop, and the loop dies with the request — which would kill the detached task that
    sends the welcome message before it ever runs.
    """
    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        settings_override(
            **{
                "env": "test",
                "database_url": db_url,
                "test_database_url": db_url,
                "session_secret": "test-session-secret-value-32-chars-min",
                "whatsapp_webhook_secret": SECRET,
                "app_public_url": PUBLIC_URL,
                "onboarding_enabled": True,
                # The cap counts every household in the database, including whatever a
                # developer already had. This is a test of onboarding, not of the cap.
                "onboarding_max_households": 100_000,
                **overrides,
            }
        )
        # The engine is cached per process and holds the OLD url; rebuild it against ours.
        app.db.get_engine.cache_clear()
        app.db.get_sessionmaker.cache_clear()
        client = TestClient(penny_app)
        client.__enter__()
        clients.append(client)
        return client

    delete_group(db_url, chat_id)
    yield _make
    for client in clients:
        client.__exit__(None, None, None)
    app.db.get_engine.cache_clear()
    app.db.get_sessionmaker.cache_clear()
    delete_group(db_url, chat_id)


# --- the feature ------------------------------------------------------------------------


def test_an_unknown_group_is_onboarded_and_the_message_that_added_penny_is_kept(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """The whole feature in one delivery, asserted on rows rather than on log lines.

    The last assertion is the point of the whole change: the credentials in the message the
    group receives actually sign in. A welcome message that reads well and does not work is
    indistinguishable from a broken product.
    """
    client = make_client()
    response = post_webhook(
        client, payload(chat_id, "3EB0ONBOARD01", "adding Penny to keep track of Mum")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "inserted": 1}

    household_ids = households_for(db_url, chat_id)
    assert len(household_ids) == 1
    household_id = household_ids[0]

    link = query(
        db_url,
        lambda s: s.scalar(
            sa.select(WhatsappLink).where(WhatsappLink.group_external_id == chat_id)
        ),
    )
    assert link.status == "linked"
    assert link.linked_at is not None

    household = query(db_url, lambda s: s.get(Household, household_id))
    # The placeholder IS the first-run-setup signal; nobody has said who is being cared for.
    assert household.care_recipient_name == "your family member"

    # The triggering message is RE-INGESTED, not dropped.
    stored = messages_for(db_url, household_id)
    assert len(stored) == 1
    assert stored[0].provider_message_id == "3EB0ONBOARD01"
    assert stored[0].text == "adding Penny to keep track of Mum"

    assert wait_for(lambda: len(sends) == 1), "the welcome message was never sent"
    sent_chat, sent_text = sends[0]
    assert sent_chat == chat_id  # the full @g.us JID, in the `phone` field
    assert PUBLIC_URL in sent_text
    assert household.username in sent_text

    passphrase = next(
        line.removeprefix("Password: ")
        for line in sent_text.splitlines()
        if line.startswith("Password: ")
    )
    assert sent_text == welcome_message(household.username, passphrase, PUBLIC_URL)

    login = client.post(
        "/api/auth/login", json={"username": household.username, "password": passphrase}
    )
    assert login.status_code == 204, "the credentials we sent the family must actually work"


def test_five_deliveries_of_one_message_make_one_household_and_one_welcome(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """THE INVARIANT THAT ROTS SILENTLY. GOWA retries 5x with backoff on any non-2xx.

    Five households would mean five passwords posted into one family's group; five welcome
    messages would mean four of them opening accounts nobody uses. Nothing in a single-delivery
    test can see either. The guarantee is three-deep — the advisory lock, the unique index on
    `whatsapp_links.group_external_id`, and `created=False` meaning "send nothing" — so this
    asserts the outcome rather than any one mechanism.
    """
    client = make_client()
    body = payload(chat_id, "3EB0REPLAY99", "Mum's GP rang, appointment moved to Thursday")

    responses = [post_webhook(client, body) for _ in range(5)]

    assert [r.status_code for r in responses] == [200] * 5
    assert responses[0].json() == {"status": "ok", "inserted": 1}
    # Replays insert nothing. They are the expected case, not an error.
    assert all(r.json() == {"status": "ok", "inserted": 0} for r in responses[1:])

    household_ids = households_for(db_url, chat_id)
    assert len(household_ids) == 1

    assert len(messages_for(db_url, household_ids[0])) == 1

    assert wait_for(lambda: len(sends) >= 1)
    # Give any second send time to be wrong before declaring there is only one.
    time.sleep(1.0)
    assert len(sends) == 1, "a replay must never post a second password into the group"


def test_concurrent_first_messages_are_all_kept_and_still_produce_one_welcome(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """THE OTHER INVARIANT THAT ROTS SILENTLY, and the one that used to be wrong.

    The replay test above is five copies of ONE message. This is the realistic shape: Penny is
    added and three people type at once, so three DIFFERENT messages arrive on a group that has
    no household yet. Exactly one delivery provisions; the other two find `created=False`
    because they waited on the advisory lock.

    `created=False` is a reason not to send a SECOND password into the family's chat. It is not
    a reason to drop the message — the household exists and is committed by then. It was
    treated as both, and two of the three messages were discarded with a 200, which is the one
    answer that guarantees GOWA never retries them. Nothing in the replay test can see it: five
    copies of one message dedup to one row whether the extra deliveries stored anything or not.

    So this asserts BOTH halves at once, because a fix to either one alone is wrong.
    """
    client = make_client()
    bodies = [
        payload(chat_id, "3EB0RACE00001", "adding Penny"),
        payload(chat_id, "3EB0RACE00002", "Mum has a GP appointment on Thursday"),
        payload(chat_id, "3EB0RACE00003", "and her new tablets started on Monday"),
    ]

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        responses = list(pool.map(lambda body: post_webhook(client, body), bodies))

    assert [r.status_code for r in responses] == [200] * len(bodies)

    household_ids = households_for(db_url, chat_id)
    assert len(household_ids) == 1, "concurrent deliveries must still make exactly one household"

    stored = {m.provider_message_id for m in messages_for(db_url, household_ids[0])}
    assert stored == {"3EB0RACE00001", "3EB0RACE00002", "3EB0RACE00003"}, (
        "a message that lost the provisioning race is still a message the family sent; "
        "answering 200 and dropping it loses it permanently"
    )

    assert wait_for(lambda: len(sends) >= 1)
    time.sleep(1.0)
    assert len(sends) == 1, "only the delivery that provisioned may post the password"


def test_a_dead_sidecar_still_leaves_the_family_with_a_household(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degrade, never crash. A welcome that cannot be delivered is a support conversation."""
    attempts: list[str] = []

    async def exploding_send(chat: str, text: str) -> gowa.GowaSendResult:
        attempts.append(chat)
        raise RuntimeError("gowa is on fire")

    monkeypatch.setattr(gowa, "send_message", exploding_send)

    client = make_client()
    response = post_webhook(client, payload(chat_id, "3EB0NOGOWA01", "hello?"))

    assert response.status_code == 200
    assert len(households_for(db_url, chat_id)) == 1
    assert wait_for(lambda: len(attempts) == 1)
    assert len(messages_for(db_url, households_for(db_url, chat_id)[0])) == 1


def test_onboarding_disabled_is_exactly_the_old_behaviour(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """The kill switch: 200, no rows, no message — what an unknown group used to get."""
    client = make_client(onboarding_enabled=False)
    response = post_webhook(client, payload(chat_id, "3EB0DISABLED", "hello"))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unknown_group"}
    assert households_for(db_url, chat_id) == []
    time.sleep(0.5)
    assert sends == []


def test_the_cap_refuses_silently_rather_than_provisioning(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """The blast radius of open onboarding is the OpenAI bill. 0 is a cap every database hits."""
    client = make_client(onboarding_max_households=0)
    response = post_webhook(client, payload(chat_id, "3EB0CAPPED01", "hello"))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unknown_group"}
    assert households_for(db_url, chat_id) == []
    time.sleep(0.5)
    assert sends == []


def test_provisioning_never_happens_before_the_guards_that_precede_it(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
) -> None:
    """ORDERING, asserted end to end. Every one of these is upstream of provisioning.

    A bad signature must not reach the parser, let alone the database. Penny's own outbound
    messages come back with `is_from_me` and would otherwise onboard the group she just
    welcomed. A direct chat has no `@g.us` and is out of scope entirely — provisioning one
    would create a household for a single person who messaged the number by mistake.
    """
    client = make_client()

    forged = post_webhook(
        client, payload(chat_id, "3EB0FORGED001", "hello"), secret="not-the-secret"
    )
    assert forged.status_code == 401

    echo = payload(chat_id, "3EB0FROMME001", "welcome message coming back at us")
    echo["payload"]["is_from_me"] = True
    assert post_webhook(client, echo).json() == {"status": "ignored", "reason": "from_me"}

    direct = payload("447700900123@s.whatsapp.net", "3EB0DIRECT001", "hello")
    assert post_webhook(client, direct).json() == {"status": "ignored", "reason": "not_a_group"}

    assert households_for(db_url, chat_id) == []
    time.sleep(0.5)
    assert sends == []


# --- the sidecar contract ---------------------------------------------------------------


async def test_send_message_recovers_when_v9_demands_a_device_id() -> None:
    """UNVERIFIABLE FROM HERE, so it must be survivable: GOWA has no public domain.

    Every `/app/*` route on the deployed v9 image needs a `device_id`. Whether `/send/*` does
    too is unknown, and the welcome message silently failing is the whole feature silently
    failing. `send_message` therefore does not guess: it sends the documented body and only
    resolves a device if GOWA answers DEVICE_ID_REQUIRED.
    """
    calls: list[dict[str, Any]] = []

    async def fake_call(method: str, path: str, **kwargs: Any) -> tuple[Any, str | None]:
        calls.append({"method": method, "path": path, **kwargs})
        if path == "/devices":
            return {"results": [{"id": "device-abc"}]}, None
        if len(calls) == 1:
            return None, gowa.DEVICE_ID_REQUIRED
        return {"message_id": "sent-1"}, None

    original = gowa._call
    gowa._call = fake_call  # type: ignore[assignment]
    try:
        result = await gowa.send_message("120363000@g.us", "hello")
    finally:
        gowa._call = original  # type: ignore[assignment]

    assert result.ok and result.message_id == "sent-1"
    sends = [c for c in calls if c["path"] == "/send/message"]
    assert len(sends) == 2
    # The field is `phone` even for a group, and it carries the full @g.us JID.
    assert sends[0]["json"] == {"phone": "120363000@g.us", "message": "hello"}
    assert sends[0].get("params") is None
    # Retry carries the id both ways v9 documents, because which one /send honours is unknown.
    assert sends[1]["params"] == {"device_id": "device-abc"}
    assert sends[1]["headers"] == {gowa.DEVICE_ID_HEADER: "device-abc"}


def test_a_gowa_error_body_yields_its_code_and_never_its_message() -> None:
    """`message` can echo the text we just sent; only the SCREAMING_SNAKE `code` may be kept."""
    import httpx

    required = httpx.Response(
        400,
        json={
            "code": "DEVICE_ID_REQUIRED",
            "message": "device_id is required via X-Device-Id header or device_id query",
        },
    )
    assert gowa._error_code(required) == "DEVICE_ID_REQUIRED"

    echoed = httpx.Response(400, json={"code": "she has a GP appointment on Thursday"})
    assert gowa._error_code(echoed) is None
    assert gowa._error_code(httpx.Response(500, text="<html>bad gateway</html>")) is None
