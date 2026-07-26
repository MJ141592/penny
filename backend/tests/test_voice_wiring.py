"""A spoken message has to arrive in the transcript as WORDS, and it has to do so in time.

Voice notes are not a niche shape in this product. In a family chat about an elderly relative,
"Mum had a fall this morning" is spoken at least as often as it is typed — by the people least
likely to type it — and until now every one of them reached extraction as the four characters
`[voice note]`. The care signal was simply not there to find.

THE TWO THINGS THAT CAN GO WRONG ARE BOTH SILENT, which is why they are pinned here rather than
left to a manual check:

1. **Extraction reads the message before the transcript lands.** Nothing raises. The run reports
   success, `extracted_at` is stamped, the transcript arrives a second later against a row
   nothing will ever look at again, and the fall in the recording is lost with no error anywhere
   in the system. The ordering that prevents it is TWO mechanisms, and both are tested:
   `extract_in_background` awaits transcription before extracting (the task that scheduled it),
   and `_awaiting_transcription` keeps an untranscribed voice note out of the cursor entirely
   (every OTHER caller — the next webhook, the import job, the tick).

2. **The handler answers slowly.** A webhook that takes longer than ~10s is a non-2xx as far as
   GOWA is concerned, and GOWA retries five times: the family gets the same message five times.
   Transcription is a download plus a model call, so it must never be on the request's clock.

And the degrade path, which is most of the value: when transcription is off, fails, or finds the
media gone, the message must render exactly as it does today — `[voice note]` — a bit later.
Nothing may raise into the webhook and nothing may be lost.

No OpenAI call is made here and no GOWA call: `transcribe_voice_note` is stubbed at the seam
`webhooks` imports it through, and `extract_messages` is a spy that records the transcript the
model WOULD have been shown. That transcript is the assertion — it is the only thing the
extraction model ever reads, so it is the only honest place to prove the words got through.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db
import app.extraction.service as service
from app.config import Settings
from app.db import to_asyncpg_url
from app.extraction.chunker import ChunkMessage, build_chunk, render_transcript
from app.extraction.runner import ExtractionRunResult
from app.main import app as penny_app
from app.models import Household, Message, WhatsappLink
from app.routers import webhooks
from app.transcription import TRANSCRIBED_MARKER, Transcript

SECRET = "a-real-webhook-secret-not-the-published-default"
LONDON = ZoneInfo("Europe/London")

# What the stub "hears". Chosen to be the thing this feature exists for: a fall, spoken.
SPOKEN = "mum had a fall this morning, she's ok but she's shaken"


# --- pure: what the model is shown ---------------------------------------------------------
#
# No database and no webhook. `render_transcript` is the entire surface the extraction model
# sees, so these four cases are the whole rendering contract for audio.


def audio(**overrides: Any) -> ChunkMessage:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "sent_at": datetime(2026, 7, 14, 20, 4, tzinfo=UTC),
        "sender_display_name": "Sarah",
        "text": None,
        "message_type": "audio",
    }
    return ChunkMessage(**(fields | overrides))


def rendered(*messages: ChunkMessage) -> str:
    return render_transcript(build_chunk(messages, ()), LONDON)


def test_a_transcribed_voice_note_renders_its_words_and_says_they_were_spoken() -> None:
    line = rendered(audio(text=SPOKEN, transcribed=True))
    # The words, on the line, where the model can find the event in them.
    assert line == f"[m1] Tue 2026-07-14 21:04 — Sarah (voice note): {SPOKEN}"
    # And no placeholder anywhere: the transcript IS the content, so nothing stands in for it.
    assert "[voice note]" not in line


def test_an_untranscribed_voice_note_is_still_the_placeholder() -> None:
    assert rendered(audio()) == "[m1] Tue 2026-07-14 21:04 — Sarah: [voice note]"


def test_a_transcript_that_came_back_empty_is_not_a_transcript() -> None:
    """Marked but wordless. Printing `Sarah (voice note):` with nothing after it tells the
    model a message exists and hides that it was audio — strictly worse than the placeholder."""
    assert rendered(audio(text="   ", transcribed=True)) == (
        "[m1] Tue 2026-07-14 21:04 — Sarah: [voice note]"
    )


def test_an_audio_file_with_a_caption_is_not_a_transcript() -> None:
    """The reason `transcribed` is a field and not `message_type == "audio" and text`.

    A forwarded recording with a caption has both, and rendering the caption as the spoken
    words would attribute a sentence to audio nobody listened to.
    """
    assert rendered(audio(text="listen to this", media_filename="voice-2026.opus")) == (
        "[m1] Tue 2026-07-14 21:04 — Sarah: [voice note: voice-2026.opus] listen to this"
    )


# --- plumbing for the database tests -------------------------------------------------------


def query(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """One read on its OWN engine, in its own loop — see `test_webhook_onboarding.query`."""

    async def run() -> Any:
        engine = create_async_engine(to_asyncpg_url(db_url))
        try:
            async with AsyncSession(engine) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def chat_id() -> str:
    return f"120363{uuid.uuid4().hex[:14]}@g.us"


@pytest.fixture
def household_id(db_url: str, chat_id: str) -> Iterator[uuid.UUID]:
    """A linked household, made directly. Onboarding is `test_webhook_onboarding`'s subject.

    THE DELETE HAS A LOCK TIMEOUT, and the reason is worth knowing before you debug it the hard
    way. Everything under test here runs in a task detached from the request, holding a
    transaction; a test that stops watching before that task commits leaves the row locked, and
    an untimed `DELETE FROM households` then waits on it for as long as the suite is willing to
    sit there. Every test below therefore waits for the work to *commit* (`wait_for_extraction`)
    rather than to start — and this timeout is what turns the day someone forgets into a failure
    with a message on it instead of a hung run.
    """
    new_id = uuid.uuid4()

    async def create(session: AsyncSession) -> None:
        session.add(
            Household(
                id=new_id,
                username=f"voice-{new_id.hex[:8]}",
                password_hash="x",
                name="Voice test",
                care_recipient_name="Margaret",
                timezone="Europe/London",
            )
        )
        session.add(
            WhatsappLink(
                household_id=new_id,
                group_external_id=chat_id,
                status="linked",
                linked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    async def drop(session: AsyncSession) -> None:
        await session.execute(sa.text("SET LOCAL lock_timeout = '15s'"))
        await session.execute(sa.delete(Household).where(Household.id == new_id))
        await session.commit()

    query(db_url, create)
    yield new_id
    query(db_url, drop)


class _ExtractionSpy:
    """Stands in for `extract_messages`. Records the messages a run was given to read."""

    def __init__(self) -> None:
        self.calls: list[list[ChunkMessage]] = []

    async def __call__(self, pending: Any, **_: Any) -> ExtractionRunResult:
        self.calls.append(list(pending))
        return ExtractionRunResult(extracted_message_ids=[m.id for m in pending])

    @property
    def seen(self) -> list[ChunkMessage]:
        """Every message every run in this test was shown, flattened."""
        return [message for call in self.calls for message in call]

    def transcript(self) -> str:
        """What the model would have read. The only thing extraction ever sees."""
        return "\n".join(rendered(m) for m in self.seen)


@pytest.fixture
def extraction(monkeypatch: pytest.MonkeyPatch) -> _ExtractionSpy:
    spy = _ExtractionSpy()
    monkeypatch.setattr(service, "extract_messages", spy)
    return spy


class _TranscriberStub:
    """`transcribe_voice_note`, without GOWA and without OpenAI.

    It writes what the real one writes — `messages.text` plus the payload marker, in the
    caller's session, without committing — because the committing is the wiring under test.
    """

    def __init__(self, *, text: str | None = SPOKEN, delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.message_ids: list[uuid.UUID] = []

    async def __call__(
        self, session: AsyncSession, message_id: uuid.UUID, **_: Any
    ) -> Transcript | None:
        self.message_ids.append(message_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.text is None:
            # GOWA down, media gone, transcription disabled: the documented `None`.
            return None
        payload = (
            await session.execute(sa.select(Message.payload).where(Message.id == message_id))
        ).scalar_one()
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id)
            .values(text=self.text, payload={**(payload or {}), TRANSCRIBED_MARKER: True})
        )
        return Transcript(
            text=self.text,
            model="gpt-4o-mini-transcribe",
            duration_seconds=8.0,
            cost_usd=Decimal("0.0005"),
        )


@pytest.fixture
def transcriber(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _TranscriberStub]:
    def _install(**kwargs: Any) -> _TranscriberStub:
        stub = _TranscriberStub(**kwargs)
        monkeypatch.setattr(webhooks, "transcribe_voice_note", stub)
        return stub

    return _install


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The sidecar. Nothing in this feature may speak — see the count assertion below."""
    sent: list[tuple[str, str]] = []

    async def fake_send(chat: str, text: str) -> Any:
        sent.append((chat, text))
        raise AssertionError("transcription must never send a WhatsApp message")

    monkeypatch.setattr(webhooks.gowa, "send_message", fake_send)
    return sent


@pytest.fixture
def client(
    db_url: str,
    household_id: uuid.UUID,
    settings_override: Callable[..., Settings],
    sends: list[tuple[str, str]],
    extraction: _ExtractionSpy,
) -> Iterator[TestClient]:
    """The real app against the real database.

    Depends on `household_id` so the ordering is right at BOTH ends: the household exists before
    the first request, and — because pytest tears fixtures down in reverse — the client (and
    with it any task still holding a transaction) is finished before anything tries to delete it.

    `with TestClient(...)` matters: outside the context manager every request gets its own
    event loop and the loop dies with the request, which would kill the detached task that
    does the transcription before it ever runs. The engine caches are cleared on the way in
    AND on the way out — they are `lru_cache`d per process and hold whatever URL and event
    loop the previous test file left behind.
    """
    settings_override(
        env="test",
        database_url=db_url,
        test_database_url=db_url,
        session_secret="test-session-secret-value-32-chars-min",
        whatsapp_webhook_secret=SECRET,
        openai_api_key="sk-test-not-a-real-key",
        # One waiting message is enough to extract. The cadence gate is `test_cadence`'s
        # subject; here it would only add a reason for this file to fail for something else.
        extract_min_unextracted=1,
        onboarding_enabled=False,
    )
    app.db.get_engine.cache_clear()
    app.db.get_sessionmaker.cache_clear()
    with TestClient(penny_app) as test_client:
        yield test_client
    app.db.get_engine.cache_clear()
    app.db.get_sessionmaker.cache_clear()


def voice_note(chat_id: str, message_id: str) -> dict[str, Any]:
    """A GOWA voice-note event. `audio` is a bare string when there is no caption."""
    return {
        "event": "message",
        "device_id": "447473209317@s.whatsapp.net",
        "payload": {
            "id": message_id,
            "chat_id": chat_id,
            "timestamp": int((datetime.now(UTC) - timedelta(days=2)).timestamp()),
            "is_from_me": False,
            "from": "447700900123@s.whatsapp.net",
            "from_name": "Sarah",
            "audio": "https://mmg.whatsapp.net/v/t62.7117-24/encrypted.enc",
        },
    }


def typed(chat_id: str, message_id: str, text: str) -> dict[str, Any]:
    body = voice_note(chat_id, message_id)
    del body["payload"]["audio"]
    body["payload"]["body"] = text
    return body


def post(client: TestClient, body: dict[str, Any]) -> Any:
    raw = json.dumps(body).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/whatsapp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Transcription and extraction run in a task detached from the request. Poll for them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def wait_for_extraction(db_url: str, household_id: uuid.UUID, count: int = 1) -> None:
    """Block until `count` messages have been extracted AND the run that did it has committed.

    Waiting on the spy instead would be waiting on the wrong edge: `extract_messages` is called
    at the START of a run, and the transaction that stamped `extracted_at` is still open for a
    while after it returns. A test that stops there leaves a live transaction holding row locks
    on a household it is about to delete, which does not fail — it hangs.
    """
    assert wait_for(lambda: extracted_count(db_url, household_id) >= count, timeout=30), (
        f"only {extracted_count(db_url, household_id)} of {count} messages were extracted"
    )


def extracted_count(db_url: str, household_id: uuid.UUID) -> int:
    return query(
        db_url,
        lambda s: s.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.household_id == household_id, Message.extracted_at.isnot(None))
        ),
    )


def stored(db_url: str, household_id: uuid.UUID) -> list[Message]:
    return list(
        query(
            db_url,
            lambda s: s.scalars(sa.select(Message).where(Message.household_id == household_id)),
        ).all()
    )


# --- the end-to-end run --------------------------------------------------------------------


@pytest.mark.db
def test_a_voice_note_reaches_the_model_as_its_words(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    transcriber: Callable[..., _TranscriberStub],
    sends: list[tuple[str, str]],
) -> None:
    """The whole point, end to end: audio in, words in the prompt."""
    stub = transcriber()

    response = post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))

    assert response.status_code == 200
    assert response.json()["inserted"] == 1
    wait_for_extraction(db_url, household_id)

    # The row: the placeholder is gone from the record itself, and it is marked so nothing
    # transcribes it twice.
    [message] = stored(db_url, household_id)
    assert message.message_type == "audio"
    assert message.text == SPOKEN
    assert message.payload[TRANSCRIBED_MARKER] is True
    assert stub.message_ids == [message.id]

    # And the transcript the extraction model was handed. This is the assertion that matters:
    # everything else could be right and this still be `[voice note]`.
    transcript = extraction.transcript()
    assert SPOKEN in transcript
    assert "(voice note): " in transcript
    assert "[voice note]" not in transcript

    # Two send sites exist in this file (welcome, reply) and transcription added neither.
    assert sends == []


@pytest.mark.db
def test_a_voice_note_that_cannot_be_transcribed_stays_a_voice_note(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    transcriber: Callable[..., _TranscriberStub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOWA down, media aged out, transcription switched off: yesterday's behaviour, later.

    The grace window is collapsed to nothing so the test does not sit through it. What it
    proves is the shape either way — an untranscribed voice note is eventually extracted, as
    the placeholder, and is never dropped on the floor.
    """
    transcriber(text=None)
    monkeypatch.setattr(service, "TRANSCRIPTION_GRACE", timedelta(0))

    response = post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))

    assert response.status_code == 200
    wait_for_extraction(db_url, household_id)

    [message] = stored(db_url, household_id)
    assert message.text is None
    assert TRANSCRIBED_MARKER not in message.payload
    assert extraction.transcript().endswith("— Sarah: [voice note]")


@pytest.mark.db
def test_an_untranscribed_voice_note_is_withheld_from_extraction_while_it_is_in_flight(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    transcriber: Callable[..., _TranscriberStub],
) -> None:
    """THE RACE, AND THE THING THAT CLOSES IT.

    A second message arriving while the first is still being transcribed triggers its own
    extraction run — and that run must not be able to see the voice note yet. Without
    `_awaiting_transcription` this is the silent failure the whole feature dies of: the second
    run reads `[voice note]`, finds nothing, stamps `extracted_at`, and the transcript that
    lands a moment later is against a row nothing will read again.

    The transcription stub holds for a second, which is roughly what a real one costs, and the
    typed message is posted into that window on purpose.
    """
    transcriber(delay=1.0)

    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    post(client, typed(chat_id, f"gowa-{uuid.uuid4().hex[:10]}", "ringing the surgery now"))

    # Both messages, extracted and committed: the interloper's run, then the voice note's own.
    wait_for_extraction(db_url, household_id, count=2)

    voice = [m for m in extraction.seen if m.message_type == "audio"]
    assert voice, "the voice note must reach extraction eventually"
    # NOT ONE of them was shown the message before its words existed.
    assert all(m.transcribed and m.text == SPOKEN for m in voice)
    assert "[voice note]" not in extraction.transcript()


@pytest.mark.db
def test_the_handler_answers_long_before_the_transcription_does(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    transcriber: Callable[..., _TranscriberStub],
) -> None:
    """A slow transcription must not be on the request's clock.

    GOWA gives up on a webhook after ~10 seconds and retries five times, so work left inline
    here is not a latency problem, it is the family receiving one message five times. Three
    seconds of stubbed transcription against a response that must come back in well under one.
    """
    transcriber(delay=3.0)

    started = time.monotonic()
    response = post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1.0, f"the webhook waited {elapsed:.2f}s for background work"
    wait_for_extraction(db_url, household_id)


@pytest.mark.db
def test_a_typed_message_is_never_sent_for_transcription(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    transcriber: Callable[..., _TranscriberStub],
) -> None:
    """Nothing but audio may reach the speech model. Scheduling text would be a bill, silently."""
    stub = transcriber()

    post(client, typed(chat_id, f"gowa-{uuid.uuid4().hex[:10]}", "she had a bad night again"))

    wait_for_extraction(db_url, household_id)
    assert stub.message_ids == []
    assert "she had a bad night again" in extraction.transcript()


@pytest.mark.db
def test_a_transcription_that_blows_up_still_lets_the_message_be_extracted(
    client: TestClient,
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    extraction: _ExtractionSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`transcribe_voice_note` promises never to raise. This is what happens when it lies.

    Not hypothetical politeness: an exception escaping the background task would abandon the
    extraction that follows it in the same task, and the message would wait for a trigger that
    may not come for hours.
    """

    async def explodes(*_: Any, **__: Any) -> Transcript | None:
        raise RuntimeError("the sidecar returned something absurd")

    monkeypatch.setattr(webhooks, "transcribe_voice_note", explodes)
    monkeypatch.setattr(service, "TRANSCRIPTION_GRACE", timedelta(0))

    response = post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))

    assert response.status_code == 200
    wait_for_extraction(db_url, household_id)
    assert extraction.transcript().endswith("— Sarah: [voice note]")


# --- the cursor, directly ------------------------------------------------------------------


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    """Never commits, so a run of this file leaves the database as it found it."""
    engine = create_async_engine(to_asyncpg_url(db_url))
    try:
        async with AsyncSession(engine) as db:
            yield db
            await db.rollback()
    finally:
        await engine.dispose()


async def add_message(
    session: AsyncSession,
    household_id: uuid.UUID,
    *,
    payload: dict[str, Any],
    text: str | None = None,
    ingested_at: datetime | None = None,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    session.add(
        Message(
            id=message_id,
            household_id=household_id,
            provider="gowa",
            provider_message_id=f"gowa-{message_id.hex[:10]}",
            content_hash=message_id.bytes,
            sender_display_name="Sarah",
            sent_at=datetime.now(UTC) - timedelta(days=1),
            message_type="audio",
            text=text,
            payload=payload,
            **({"ingested_at": ingested_at} if ingested_at else {}),
        )
    )
    await session.flush()
    return message_id


@pytest.mark.db
async def test_the_cursor_withholds_a_fresh_voice_note_and_releases_a_stale_one(
    # `household_id` FIRST: same-scope fixtures are torn down in reverse order of setup, and the
    # rows below are only rolled back when `session` closes. Asking for the household afterwards
    # would delete it while this transaction still holds locks on its messages.
    household_id: uuid.UUID,
    session: AsyncSession,
    db_url: str,
    settings_override: Callable[..., Settings],
) -> None:
    """`_awaiting_transcription`, stated as the three rows it decides between.

    The expiry is the half that is easy to leave out and expensive to leave out: without it, a
    household whose transcription is switched off or whose sidecar is down would have every
    voice note it ever received withheld from the feed permanently, and nothing would say so.
    """
    settings_override(database_url=db_url, test_database_url=db_url, extract_min_unextracted=1)

    in_flight = await add_message(session, household_id, payload={})
    done = await add_message(session, household_id, payload={TRANSCRIBED_MARKER: True}, text=SPOKEN)
    gave_up = await add_message(
        session,
        household_id,
        payload={},
        ingested_at=datetime.now(UTC) - service.TRANSCRIPTION_GRACE - timedelta(seconds=30),
    )

    pending = await service._unextracted(session, household_id)
    by_id = {message.id: message for message in pending}

    assert in_flight not in by_id, "a voice note being transcribed right now must not extract"
    assert by_id[done].transcribed is True
    assert by_id[done].text == SPOKEN
    assert by_id[gave_up].transcribed is False
