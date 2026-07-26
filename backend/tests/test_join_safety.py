"""THE TESTS THAT EXIST BECAUSE OF AN INCIDENT.

Penny is paired to a REAL WhatsApp account that was already a member of several unrelated group
chats. Onboarding provisioned a household on ANY message from ANY unknown group, so a login
password was posted into other people's conversations — three households, two accidental. In the
words of someone who was in one of those groups: "It sent the message to all groups i'm in at the
same time, but i'm not sure why".

So the property under test is not "onboarding works". It is **Penny never says a word to a group
she was not just added to**, and every test below is a way that could stop being true:

    a message from an unknown group          -> nothing stored, nothing sent, no household
    a join during the startup quiet period   -> recorded, no household, nothing sent
    a join for a group id we have seen before-> no household, nothing sent
    a group.participants event, ever         -> no household, nothing sent
    a genuine join (unseen id, quiet period over) -> household + welcome, exactly once
    that same join replayed 4x               -> still one household, one welcome
    an @-mention in a LINKED group           -> reply scheduled
    an @-mention in an UNLINKED group        -> nothing stored, nothing sent
    a normal message in a linked group       -> ingested, no reply

These run against REAL Postgres through the REAL app with REAL signed bodies, and skip without
`PENNY_TEST_DATABASE_URL`. Rows and sends are what is asserted, never log lines: the incident was
invisible in the logs until a human noticed and said something.

The sidecar, the LLM and the extractor are stubs — `gowa.send_message`, `answer_mention` and
`run_extraction_for_household` are monkeypatched — so nothing here reaches the network. The
`sends` list IS the family's group chat: `assert sends == []` is the whole point of this file.

WHY `in_startup_quiet_period` IS PATCHED RATHER THAN WAITED OUT. It is a function of how long
this *process* has been alive, and a test process is always young, so left alone every join in
this file would be suppressed and the genuine-join tests would pass without proving anything.
Patching it makes the gate an explicit input: each test states which side of the window it is on.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db
from app import gowa, mentions
from app.config import Settings
from app.db import to_asyncpg_url
from app.extraction.service import RunSummary
from app.main import app as penny_app
from app.models import Household, Message, WhatsappLink
from app.routers import webhooks

SECRET = "a-real-webhook-secret-not-the-published-default"
PUBLIC_URL = "https://pennyai.chat"
# The paired account, as it arrives on every webhook envelope's `device_id`. The digits are the
# ones that showed up in a real mention: "@17473209317 who are you".
OWN_JID = "17473209317@s.whatsapp.net"

pytestmark = pytest.mark.db


# --- plumbing ---------------------------------------------------------------------------


def query(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """Run one read on its OWN engine, in its own loop.

    The app's engine lives in the TestClient's portal thread and its asyncpg connections are
    bound to that loop; borrowing it from the test thread gives "attached to a different loop"
    at random.
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
    """Sends happen on a task detached from the request. Poll for them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def assert_stays_silent(sends: list[tuple[str, str]], seconds: float = 1.0) -> None:
    """A NEGATIVE about an asynchronous send, which needs time to be wrong in.

    Asserting `sends == []` the instant a 200 comes back proves nothing: the send it is trying to
    rule out is scheduled to happen slightly later. So give it a window in which to misbehave.
    """
    time.sleep(seconds)
    assert sends == [], f"Penny sent {len(sends)} message(s) into a group she must be silent in"


def message_body(chat_id: str, message_id: str, text: str) -> dict[str, Any]:
    return {
        "event": "message",
        "device_id": OWN_JID,
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


def join_body(chat_id: str, event: str = "group.joined") -> dict[str, Any]:
    """A `group.joined` as GOWA builds it — shape read from `event_group.go`, not from the docs.

    This is EXACTLY the body whatsmeow also re-emits, once per group, during app-state sync for
    groups the account was already in. Nothing in it distinguishes the two cases; that is the
    whole reason the gates are circumstantial.
    """
    return {
        "event": event,
        "device_id": OWN_JID,
        "payload": {
            "chat_id": chat_id,
            "type": "join",
            "jids": [OWN_JID],
            "group_name": "Mum's care",
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
    """A group id of this test's own.

    Freshness is load-bearing here, not just hygiene: the ledger is permanent by design, so a
    reused id would be "already known" on the second run and the genuine-join tests would go
    green for the wrong reason forever after.
    """
    return f"120363{uuid4().hex[:14]}@g.us"


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The family's group chat, as a list. Every entry is a message a real person would read."""
    sent: list[tuple[str, str]] = []

    async def fake_send(chat: str, text: str) -> gowa.GowaSendResult:
        sent.append((chat, text))
        return gowa.GowaSendResult(ok=True, message_id=f"stub-{len(sent)}")

    monkeypatch.setattr(gowa, "send_message", fake_send)
    return sent


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The assistant, stubbed. Records what it was asked; never calls OpenAI."""
    asked: list[str] = []

    async def fake_answer(session: AsyncSession, household: Household, message_text: str) -> str:
        asked.append(message_text)
        return f"Penny here. You asked: {message_text}"

    monkeypatch.setattr(webhooks, "answer_mention", fake_answer)
    return asked


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_extraction(session: AsyncSession, household_id: UUID, **_: Any) -> RunSummary:
        return RunSummary(household_id=household_id, ran=False)

    monkeypatch.setattr(webhooks, "run_extraction_for_household", fake_extraction)


@pytest.fixture
def joined_long_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup quiet period is over: gate (b) is open, gate (a) decides."""
    monkeypatch.setattr(webhooks, "in_startup_quiet_period", lambda: False)


@pytest.fixture
def just_reconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process is seconds old and app state is arriving: gate (b) is shut."""
    monkeypatch.setattr(webhooks, "in_startup_quiet_period", lambda: True)


@pytest.fixture(autouse=True)
def _reset_process_state() -> Iterator[None]:
    """The paired-number cache and the reply limiter are module globals; isolate the tests."""
    webhooks._own_jid = None
    webhooks._own_jid_expires_at = 0.0
    webhooks._reply_hits.clear()
    yield
    webhooks._own_jid = None
    webhooks._own_jid_expires_at = 0.0
    webhooks._reply_hits.clear()


@pytest.fixture
def make_client(
    db_url: str,
    settings_override: Callable[..., Settings],
    chat_id: str,
    sends: list[tuple[str, str]],
    no_llm: None,
) -> Iterator[Callable[..., TestClient]]:
    """A TestClient wired to the real database, with onboarding ON unless a test says otherwise.

    `with TestClient(...)` matters: outside the context manager every request gets its own event
    loop and the loop dies with the request, which would kill the detached task under test.
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
                "onboarding_max_households": 100_000,
                # Gate (c) OFF by default here, so these tests stay about gates (a) and (b).
                # Left on, every genuine-join test would hold for the burst window before
                # saying anything and would time out waiting. The burst gate has its own tests
                # at the end of this file, which switch it back on explicitly.
                "join_burst_window_seconds": 0.0,
                **overrides,
            }
        )
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


def link_group(
    client: TestClient,
    db_url: str,
    chat_id: str,
    sends: list[tuple[str, str]],
    replies: list[str],
) -> UUID:
    """Get a group legitimately linked, the only way there is: a genuine join.

    Used as setup by the mention tests, and it is deliberately the real path rather than an
    INSERT — a household that appeared by any other route would not prove the mention tests are
    running against the same thing production has.
    """
    assert post_webhook(client, join_body(chat_id)).json() == {"status": "ok", "onboarded": True}
    assert wait_for(lambda: len(sends) == 1), "the welcome never arrived"
    household_ids = households_for(db_url, chat_id)
    assert len(household_ids) == 1
    sends.clear()
    replies.clear()
    return household_ids[0]


# --- a message must never provision or send ----------------------------------------------


def test_a_message_from_an_unknown_group_stores_nothing_and_says_nothing(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    joined_long_ago: None,
) -> None:
    """THE INCIDENT, AS A TEST. This exact delivery is what leaked a password.

    A group the paired account has been sitting in for months says something ordinary. Before
    this change that created a household and posted credentials into the chat. It is now
    indistinguishable from noise: no household, no rows, no message, 200.

    Note that onboarding is ENABLED here. The kill switch is not what makes this safe.
    """
    client = make_client()
    response = post_webhook(
        client, message_body(chat_id, "3EB0STRANGER1", "anyone free for five-a-side thursday?")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unknown_group"}
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)


def test_an_at_mention_in_an_unlinked_group_stores_nothing_and_says_nothing(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    replies: list[str],
    joined_long_ago: None,
) -> None:
    """Being spoken to is not the same as being invited.

    This is the tempting version of the bug: someone in a group Penny is already in notices the
    number and pokes it. Answering would mean an unlinked group can trigger an outbound message,
    which is the property that has to be zero — and replying "I'm not set up here" is still a
    message in a stranger's chat. Silence.
    """
    client = make_client()
    response = post_webhook(
        client, message_body(chat_id, "3EB0POKE00001", f"@{OWN_JID.split('@')[0]} who are you")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unknown_group"}
    assert households_for(db_url, chat_id) == []
    assert replies == []
    assert_stays_silent(sends)


# --- the two gates on group.joined --------------------------------------------------------


def test_a_join_inside_the_startup_quiet_period_is_recorded_but_never_provisions(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE (b), and the one that covers a first deploy against an empty ledger.

    whatsmeow re-emits `JoinedGroup` for every group the account was already in whenever app
    state syncs, which is seconds after every connect and every restart. GOWA has no guard for
    it. So a join arriving in that window is treated as sync, unconditionally.

    THE SECOND HALF IS THE POINT: it is RECORDED. The proof is the follow-up join sent after the
    window has passed — it is refused as `already_known`, which can only happen if the
    suppressed event wrote to the ledger. Without that, every restart would be a fresh chance to
    provision the same stranger's group.
    """
    client = make_client()
    monkeypatch.setattr(webhooks, "in_startup_quiet_period", lambda: True)
    response = post_webhook(client, join_body(chat_id))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "sync_suppressed"}
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)

    # The window closes. The very same event is now permanently inoculated.
    monkeypatch.setattr(webhooks, "in_startup_quiet_period", lambda: False)
    later = post_webhook(client, join_body(chat_id))

    assert later.json() == {"status": "ignored", "reason": "already_known"}
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)


def test_a_join_for_a_group_we_have_already_seen_never_provisions(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    joined_long_ago: None,
) -> None:
    """GATE (a), fed by ORDINARY TRAFFIC — the half that makes the pre-existing chats safe.

    The realistic sequence for one of the groups in the incident: it has been chatting away for
    months (so we have seen its id), and then a reconnect fires a join event for it. That join
    must be refused even though the quiet period is long over, because we have evidence this
    group is not new.
    """
    client = make_client()

    # Months of ordinary traffic from a group nobody linked. Stored nowhere, but SEEN.
    for i in range(3):
        assert post_webhook(client, message_body(chat_id, f"3EB0OLDCHAT{i}", "hi")).json() == {
            "status": "ignored",
            "reason": "unknown_group",
        }

    response = post_webhook(client, join_body(chat_id))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "already_known"}
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)


def test_a_participants_event_is_recorded_and_never_provisions(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    joined_long_ago: None,
) -> None:
    """`group.participants` fires on every membership change in every group Penny sits in.

    It used to be able to provision when our own JID appeared in `jids`. It cannot now, because
    "our jid is in a participants list" is true of the group Penny has been in for a year every
    time anyone else joins it. It still feeds the ledger, which is the only job it has left.
    """
    client = make_client()
    response = post_webhook(client, join_body(chat_id, event="group.participants"))

    assert response.json() == {"status": "ignored", "reason": "not_a_join_event"}
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)

    # It was recorded: a real join event for the same id is now refused.
    assert post_webhook(client, join_body(chat_id)).json() == {
        "status": "ignored",
        "reason": "already_known",
    }
    assert households_for(db_url, chat_id) == []
    assert_stays_silent(sends)


# --- a genuine join, which must still work ------------------------------------------------


def test_a_genuine_join_provisions_once_and_welcomes_once(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    joined_long_ago: None,
) -> None:
    """The feature still has to happen, or the answer to the incident is just "turn it off".

    An id nobody has ever seen, arriving outside the startup window: both gates open, one
    household, one welcome, and the credentials in it must actually sign in — a welcome message
    that reads well and does not work is indistinguishable from a broken product.
    """
    client = make_client()
    response = post_webhook(client, join_body(chat_id))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "onboarded": True}

    household_ids = households_for(db_url, chat_id)
    assert len(household_ids) == 1
    household = query(db_url, lambda s: s.get(Household, household_ids[0]))

    assert wait_for(lambda: len(sends) == 1), "the welcome message was never sent"
    sent_chat, sent_text = sends[0]
    assert sent_chat == chat_id
    assert household.username in sent_text

    passphrase = next(
        line.removeprefix("Password: ")
        for line in sent_text.splitlines()
        if line.startswith("Password: ")
    )
    login = client.post(
        "/api/auth/login", json={"username": household.username, "password": passphrase}
    )
    assert login.status_code == 204, "the credentials we sent the family must actually work"


def test_replaying_a_genuine_join_four_more_times_changes_nothing(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    joined_long_ago: None,
) -> None:
    """GOWA retries 5x with backoff, and a re-delivered join is the expected case.

    Five households would be five passwords in one family's chat on the day they meet the
    product. Gate (a) alone answers this now — the first delivery recorded the id — with the
    advisory lock in `provision_for_group` still underneath for the concurrent case.
    """
    client = make_client()
    body = join_body(chat_id)

    responses = [post_webhook(client, body) for _ in range(5)]

    assert [r.status_code for r in responses] == [200] * 5
    assert responses[0].json() == {"status": "ok", "onboarded": True}
    assert all(r.json() == {"status": "ignored", "reason": "already_known"} for r in responses[1:])
    assert len(households_for(db_url, chat_id)) == 1

    assert wait_for(lambda: len(sends) >= 1)
    time.sleep(1.0)
    assert len(sends) == 1, "a replayed join must never post a second password into the group"


# --- @-mentions in a linked group ---------------------------------------------------------


def test_a_normal_message_in_a_linked_group_is_ingested_without_a_reply(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    replies: list[str],
    joined_long_ago: None,
) -> None:
    """The overwhelmingly common case, and it must stay completely silent.

    A family talks all day. Penny reads everything and answers nothing unless addressed — that is
    what keeps outbound volume near zero, which is what keeps the paired account unbanned.
    """
    client = make_client()
    household_id = link_group(client, db_url, chat_id, sends, replies)

    response = post_webhook(
        client, message_body(chat_id, "3EB0NORMAL001", "Mum's GP rang, appointment moved")
    )

    assert response.json() == {"status": "ok", "inserted": 1, "reply": "not_mentioned"}
    stored = messages_for(db_url, household_id)
    assert [m.text for m in stored] == ["Mum's GP rang, appointment moved"]
    assert replies == []
    assert_stays_silent(sends)


def test_an_at_mention_in_a_linked_group_is_ingested_and_answered(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    replies: list[str],
    joined_long_ago: None,
) -> None:
    """The exact body a real mention arrived in: the number, inline, in the message text.

    Both halves are asserted. The message is INGESTED — a question to Penny is still something
    the family said, and dropping it would put a hole in the timeline. And the answer goes back
    to the group it came from, once.
    """
    client = make_client()
    household_id = link_group(client, db_url, chat_id, sends, replies)
    text = "@17473209317 when is Mum's next appointment?"

    response = post_webhook(client, message_body(chat_id, "3EB0MENTION01", text))

    assert response.json() == {"status": "ok", "inserted": 1, "reply": "scheduled"}
    assert [m.text for m in messages_for(db_url, household_id)] == [text]

    assert wait_for(lambda: len(sends) == 1), "the reply was never sent"
    sent_chat, sent_text = sends[0]
    assert sent_chat == chat_id
    assert replies == [text]
    assert sent_text == f"Penny here. You asked: {text}"

    # A retried delivery of the same mention must not produce a second answer.
    replay = post_webhook(client, message_body(chat_id, "3EB0MENTION01", text))
    assert replay.json() == {"status": "ok", "inserted": 0, "reply": "not_mentioned"}
    time.sleep(1.0)
    assert len(sends) == 1, "a replayed mention must not be answered twice"


def test_replies_are_rate_limited_per_household(
    db_url: str,
    chat_id: str,
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    replies: list[str],
    joined_long_ago: None,
) -> None:
    """A few an hour. Every reply is an OpenAI call and an outbound message from a real account.

    Over the limit Penny says NOTHING — no apology, because an apology is itself an outbound
    message and the thing being limited is outbound messages. The overflow messages are still
    ingested: rate limiting the answer must not lose the question.
    """
    client = make_client()
    household_id = link_group(client, db_url, chat_id, sends, replies)
    limit = webhooks.MAX_REPLIES_PER_HOUSEHOLD

    outcomes = [
        post_webhook(
            client, message_body(chat_id, f"3EB0FLOOD{i:04d}", f"@17473209317 question {i}")
        ).json()["reply"]
        for i in range(limit + 3)
    ]

    assert outcomes == ["scheduled"] * limit + ["rate_limited"] * 3
    assert wait_for(lambda: len(sends) == limit)
    time.sleep(1.0)
    assert len(sends) == limit
    # Every message is still in the record, answered or not.
    assert len(messages_for(db_url, household_id)) == limit + 3


# --- what counts as a mention (no database) -----------------------------------------------


def test_a_bare_number_in_the_body_is_the_mention_production_actually_sends() -> None:
    """Captured live: "@17473209317 who are you". The client shows a name; the wire has digits."""
    assert mentions.mentions_penny("@17473209317 who are you", {}, OWN_JID)
    assert mentions.mentions_penny("hi @17473209317, any news?", {}, OWN_JID)
    assert mentions.mentions_penny("@+1 747 320 9317 hello", {}, OWN_JID)
    assert mentions.mentions_penny("@penny how is Mum", {}, None)


def test_a_mention_of_somebody_else_is_not_a_mention_of_penny() -> None:
    """The failure that matters: Penny joining a conversation she was not part of."""
    assert not mentions.mentions_penny("@447700900123 can you call Mum", {}, OWN_JID)
    assert not mentions.mentions_penny("Penny said she would come at 4", {}, OWN_JID)
    assert not mentions.mentions_penny("email her at mum@example.com", {}, OWN_JID)
    assert not mentions.mentions_penny("@2024 budget review", {}, OWN_JID)
    assert not mentions.mentions_penny(None, {}, OWN_JID)
    # No paired number known: a number can never match, so nothing is invented.
    assert not mentions.mentions_penny("@17473209317 hello", {}, None)


def test_the_metadata_path_is_a_bonus_and_never_the_only_signal() -> None:
    """`mentioned_jid` may or may not exist; read it if it does, survive if it does not."""
    assert mentions.mentions_penny("who are you", {"mentioned_jid": [OWN_JID]}, OWN_JID)
    assert mentions.mentions_penny(
        "who are you", {"context_info": {"mentionedJid": [OWN_JID]}}, OWN_JID
    )
    assert not mentions.mentions_penny(
        "who are you", {"mentioned_jid": ["447700900123@s.whatsapp.net"]}, OWN_JID
    )
    # The `from` field is our own number on an echoed message and must NOT read as a mention.
    assert not mentions.mentions_penny("hello", {"from": OWN_JID, "chat_id": "1@g.us"}, OWN_JID)


def test_the_cheap_prefilter_never_rejects_something_the_real_check_would_accept() -> None:
    """`has_mention_marker` is what keeps the sidecar off the hot path; it must not lose one."""
    for text in ["@17473209317 hi", "@penny hi", "hi @ 17473209317"]:
        assert mentions.has_mention_marker(text, {})
    assert mentions.has_mention_marker(None, {"mentioned_jid": [OWN_JID]})
    assert not mentions.has_mention_marker("Mum's appointment moved to Thursday", {})


# --- gate (c): the burst -------------------------------------------------------------------
#
# THE GATE THAT COVERS THE DAY THIS SHIPS. Gates (a) and (b) can both be open at the same time,
# and the state in which that happens is the state production is in on the first deploy: an
# EMPTY `known_groups` makes every pre-existing group a "first sighting", and a whatsmeow
# app-state burst that lands a little more than `startup_quiet_period_seconds` after boot has
# already cleared the quiet window. That combination was reproduced against the shipped code —
# eight `group.joined` events produced eight households and eight passwords, which is the
# original incident exactly.
#
# The signal neither of the other gates uses is CARDINALITY: a human adds Penny to one group at
# a time, whereas app state replays every group the account already belonged to at once.


def test_a_burst_of_joins_on_an_empty_ledger_sends_nothing(
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    db_url: str,
    joined_long_ago: None,
    no_llm: None,
) -> None:
    """THE REGRESSION TEST FOR THE REPRODUCTION. Eight pre-existing groups, one reconnect.

    Every one of them is unknown (fresh ledger, gate (a) open) and the quiet period is over
    (gate (b) open). Before gate (c) this produced eight welcomes into eight strangers' chats.
    """
    client = make_client(join_burst_window_seconds=2.0)
    groups = [f"120363{uuid4().hex[:14]}@g.us" for _ in range(8)]
    try:
        for chat_id in groups:
            post_webhook(client, join_body(chat_id))
        # Longer than the window, so a welcome being merely SLOW cannot pass for one being
        # withheld: by now the held first join has woken up, looked again and seen the burst.
        assert_stays_silent(sends, seconds=4.0)
        # At most the first of the burst may have provisioned — it was genuinely alone at the
        # instant it arrived, and the hold in `send_welcome` is what stops it SPEAKING. The
        # other seven must not even exist. An orphan household is deletable; a password is not.
        provisioned = [g for g in groups if households_for(db_url, g)]
        assert len(provisioned) <= 1, f"{len(provisioned)} households from one sync burst"
    finally:
        for chat_id in groups:
            delete_group(db_url, chat_id)


def test_the_first_group_of_a_burst_is_also_silent(
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    db_url: str,
    joined_long_ago: None,
    no_llm: None,
) -> None:
    """The one gate (c) cannot catch synchronously — it IS alone when it arrives.

    The welcome is held for the window and re-counted; the groups that land a moment later are
    what turn it into a refusal. Without the hold, group one still leaks a password.
    """
    client = make_client(join_burst_window_seconds=2.0)
    first = f"120363{uuid4().hex[:14]}@g.us"
    rest = [f"120363{uuid4().hex[:14]}@g.us" for _ in range(3)]
    try:
        response = post_webhook(client, join_body(first))
        # It is accepted at the door — nothing about it looks wrong yet.
        assert response.json() == {"status": "ok", "onboarded": True}
        for chat_id in rest:
            post_webhook(client, join_body(chat_id))
        assert_stays_silent(sends, seconds=4.0)
    finally:
        for chat_id in [first, *rest]:
            delete_group(db_url, chat_id)


def test_a_solitary_genuine_join_still_gets_its_welcome_with_the_burst_gate_on(
    make_client: Callable[..., TestClient],
    sends: list[tuple[str, str]],
    db_url: str,
    chat_id: str,
    joined_long_ago: None,
    no_llm: None,
) -> None:
    """The gate must not be a blanket off switch: one family, alone, is still welcomed."""
    client = make_client(join_burst_window_seconds=1.0)
    try:
        assert post_webhook(client, join_body(chat_id)).json() == {
            "status": "ok",
            "onboarded": True,
        }
        assert wait_for(lambda: len(sends) == 1), "a solitary join must still be welcomed"
        assert sends[0][0] == chat_id
        assert len(households_for(db_url, chat_id)) == 1
    finally:
        delete_group(db_url, chat_id)
