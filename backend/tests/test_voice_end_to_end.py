"""The whole voice-note path, driven from the outside, on real Postgres.

This file deliberately stubs the SMALLEST possible surface — one OpenAI method — and runs
everything else for real: the real webhook with a real HMAC signature, the real ingest, the
real `gowa.download_media` over real HTTP against a fake sidecar process, the real
`transcribe_voice_note`, the real pricing, the real `llm_runs` write, the real extraction
cursor and the real chunker.

`tests/test_transcription.py` and `tests/test_voice_wiring.py` each prove one half with the
other half stubbed. Neither one can catch a mismatch BETWEEN the halves — a production call
site that passes the wrong id, a marker written under one name and read under another, a
`gowa_url` that is never consulted — because in both of them the seam under suspicion is the
thing that was replaced. So this file replaces neither.

WHAT IS STUBBED, AND WHY ONLY THIS. `app.transcription.OpenAITranscriber` is swapped for a
class with the same one method. That is the single point where money is spent and the only
thing in the path we are forbidden to exercise. Note that it is patched as a MODULE ATTRIBUTE,
not injected: `transcribe_voice_note(session, message_id)` is called by production with two
positional arguments and constructs its own transcriber, so patching the attribute is what
makes these tests cover the production call, not a test-only overload of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db
import app.extraction.service as service
import app.transcription as transcription
from app import gowa
from app.config import Settings
from app.db import to_asyncpg_url
from app.extraction.chunker import ChunkMessage, build_chunk, render_transcript
from app.extraction.runner import ExtractionRunResult
from app.main import app as penny_app
from app.models import Household, LlmRun, Message, WhatsappLink
from app.openai_client import get_openai_client
from app.routers import webhooks
from app.transcription import TRANSCRIBE_PURPOSE, TRANSCRIBED_MARKER, RawTranscription

SECRET = "a-real-webhook-secret-not-the-published-default"
LONDON = ZoneInfo("Europe/London")

# The sentence this whole feature exists to stop losing.
SPOKEN = "mum had a fall this morning, she's ok but she's shaken"

# 20 seconds of "audio". The bytes are never decoded — the transcriber is stubbed — but they
# are the thing the byte cap, the multipart upload and the `llm_runs` byte count all measure,
# so they are a real, non-trivial length rather than b"x".
AUDIO_BYTES = b"OggS" + bytes(range(256)) * 128
DECLARED_SECONDS = 20.0


# --- the fake sidecar ----------------------------------------------------------------------


class _FakeGowa:
    """A real HTTP server on 127.0.0.1 speaking GOWA's media route.

    A real socket rather than a mocked transport because the thing most likely to be wrong
    here is not our logic — it is the HTTP shape: the URL we build, the auth header, whether a
    streamed body is read to completion, whether a `content-type` with parameters survives.
    A mock that answers whatever it is asked cannot fail any of those.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.status = 200
        self.content_type = "audio/ogg; codecs=opus"
        self.body = AUDIO_BYTES
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # stdlib's spelling, not ours
                server.requests.append(self.path)
                if server.status != 200:
                    payload = json.dumps({"code": "NOT_FOUND", "message": "gone"}).encode()
                    self.send_response(server.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", server.content_type)
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *_: Any) -> None:
                """Silence. The stdlib handler writes every request to stderr."""

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def downloads(self) -> list[str]:
        return [path for path in self.requests if path.endswith("/download")]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def gowa_server() -> Iterator[_FakeGowa]:
    server = _FakeGowa()
    yield server
    server.close()


@pytest.fixture
def dead_port() -> str:
    """A URL nothing is listening on. Bound and closed, so the port is real and refuses."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --- the one stub --------------------------------------------------------------------------


class _StubTranscriber:
    """`OpenAITranscriber` without OpenAI. Records what it was handed."""

    calls: list[dict[str, Any]] = []  # class-level on purpose, see below
    text: str = SPOKEN

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, content_type: str
    ) -> RawTranscription:
        type(self).calls.append(
            {
                "model": model,
                "byte_count": len(audio),
                "filename": filename,
                "content_type": content_type,
            }
        )
        return RawTranscription(text=type(self).text, duration_seconds=None)


@pytest.fixture
def transcriber(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_StubTranscriber]]:
    """Class-level state because production INSTANTIATES the class itself.

    `transcribe_voice_note` does `transcriber or OpenAITranscriber()`, so the test never holds
    the instance that gets used — which is precisely the property being tested. The counter
    therefore lives on the class.
    """
    _StubTranscriber.calls = []
    _StubTranscriber.text = SPOKEN
    monkeypatch.setattr(transcription, "OpenAITranscriber", _StubTranscriber)
    yield _StubTranscriber
    _StubTranscriber.calls = []


# --- database plumbing ---------------------------------------------------------------------


def query(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """One read on its OWN engine and loop, so it can never see the app's uncommitted work."""

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
    new_id = uuid.uuid4()

    async def create(session: AsyncSession) -> None:
        session.add(
            Household(
                id=new_id,
                username=f"e2e-{new_id.hex[:8]}",
                password_hash="x",
                name="E2E voice",
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
        # Timed, because a background task that never finished still holds row locks and an
        # untimed DELETE would hang the suite rather than fail it.
        await session.execute(sa.text("SET LOCAL lock_timeout = '15s'"))
        await session.execute(sa.delete(Household).where(Household.id == new_id))
        await session.commit()

    query(db_url, create)
    yield new_id
    query(db_url, drop)


class _ExtractionSpy:
    """Stands in for `extract_messages` — the only other place money would be spent.

    It records the `ChunkMessage`s a run was given, which is exactly what the prompt is
    rendered from, so `transcript()` is a faithful reproduction of what the model would read.
    """

    def __init__(self) -> None:
        self.calls: list[list[ChunkMessage]] = []

    async def __call__(self, pending: Any, **_: Any) -> ExtractionRunResult:
        self.calls.append(list(pending))
        return ExtractionRunResult(extracted_message_ids=[m.id for m in pending])

    @property
    def seen(self) -> list[ChunkMessage]:
        return [message for call in self.calls for message in call]

    def transcript(self) -> str:
        if not self.seen:
            return ""
        return render_transcript(build_chunk(self.seen, ()), LONDON)


@pytest.fixture
def extraction(monkeypatch: pytest.MonkeyPatch) -> _ExtractionSpy:
    spy = _ExtractionSpy()
    monkeypatch.setattr(service, "extract_messages", spy)
    return spy


@pytest.fixture
def no_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(chat: str, text: str) -> Any:
        raise AssertionError("nothing in the voice-note path may send a WhatsApp message")

    monkeypatch.setattr(webhooks.gowa, "send_message", fake_send)


@pytest.fixture
def make_client(
    db_url: str,
    household_id: uuid.UUID,
    settings_override: Callable[..., Settings],
    extraction: _ExtractionSpy,
    no_sends: None,
) -> Iterator[Callable[..., TestClient]]:
    """Builds the real app against the real database, with per-test settings.

    A factory rather than a fixture because two of the eight steps below are ABOUT a setting
    (`transcribe_voice_notes=False`, a dead `gowa_url`), and a setting applied after the app
    has started is a setting the background task may or may not have read yet.
    """
    clients: list[TestClient] = []

    def _build(**overrides: Any) -> TestClient:
        settings_override(
            env="test",
            database_url=db_url,
            test_database_url=db_url,
            session_secret="test-session-secret-value-32-chars-min",
            whatsapp_webhook_secret=SECRET,
            openai_api_key="sk-test-not-a-real-key",
            gowa_basic_auth="penny:secret",
            # One waiting message is enough. The cadence gate has its own file; here it would
            # only be a second reason for these tests to fail.
            extract_min_unextracted=1,
            onboarding_enabled=False,
            **overrides,
        )
        app.db.get_engine.cache_clear()
        app.db.get_sessionmaker.cache_clear()
        client = TestClient(penny_app)
        client.__enter__()
        clients.append(client)
        return client

    yield _build
    for client in clients:
        client.__exit__(None, None, None)
    app.db.get_engine.cache_clear()
    app.db.get_sessionmaker.cache_clear()


# --- the wire ------------------------------------------------------------------------------


def voice_note(chat_id: str, message_id: str, **payload: Any) -> dict[str, Any]:
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
            # v9 emits the media field as a bare URL string when there is no caption. That URL
            # is an ENCRYPTED blob and is never fetched — it is here because it is what really
            # arrives, and something reading it by mistake should be caught by these tests.
            "audio": "https://mmg.whatsapp.net/v/t62.7117-24/encrypted.enc",
            "duration": DECLARED_SECONDS,
            **payload,
        },
    }


def typed(chat_id: str, message_id: str, text: str) -> dict[str, Any]:
    body = voice_note(chat_id, message_id)
    del body["payload"]["audio"]
    del body["payload"]["duration"]
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


def wait_for(predicate: Callable[[], bool], timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def messages(db_url: str, household_id: uuid.UUID) -> list[Message]:
    return list(
        query(
            db_url,
            lambda s: s.scalars(
                sa.select(Message)
                .where(Message.household_id == household_id)
                .order_by(Message.sent_at, Message.id)
            ),
        ).all()
    )


def runs(db_url: str, household_id: uuid.UUID, purpose: str) -> list[LlmRun]:
    return list(
        query(
            db_url,
            lambda s: s.scalars(
                sa.select(LlmRun).where(
                    LlmRun.household_id == household_id, LlmRun.purpose == purpose
                )
            ),
        ).all()
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


def wait_for_extraction(db_url: str, household_id: uuid.UUID, count: int = 1) -> None:
    """Wait for the COMMIT, not for the call.

    `extract_messages` is invoked at the start of a run whose transaction stays open for a
    while afterwards; a test that stops at the spy leaves that transaction holding row locks on
    a household the teardown is about to delete, which hangs rather than fails.
    """
    assert wait_for(lambda: extracted_count(db_url, household_id) >= count), (
        f"only {extracted_count(db_url, household_id)} of {count} messages were extracted"
    )


def backdate(db_url: str, household_id: uuid.UUID, minutes: int) -> None:
    """Age every message past `TRANSCRIPTION_GRACE`.

    The grace window is what withholds an untranscribed voice note from extraction, and it is
    measured against the DATABASE clock — so this moves `ingested_at` with `now()`, not with a
    Python `datetime`, for the same reason the production clause does.
    """

    async def run(session: AsyncSession) -> None:
        await session.execute(
            sa.update(Message)
            .where(Message.household_id == household_id)
            .values(ingested_at=sa.func.now() - timedelta(minutes=minutes))
        )
        await session.commit()

    query(db_url, run)


# --- 1-5: audio in, words out, charged exactly once ------------------------------------------


@pytest.mark.db
def test_voice_note_is_stored_transcribed_billed_and_read_as_words(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
    extraction: _ExtractionSpy,
) -> None:
    """Steps 1-5 in one run, because they are one run: splitting them would re-prove the
    arrival four times and prove the SEQUENCE none of them."""
    client = make_client(gowa_url=gowa_server.url)
    provider_id = f"gowa-{uuid.uuid4().hex[:10]}"

    response = post(client, voice_note(chat_id, provider_id))

    # The 200 is immediate and unconditional: transcription and extraction are both behind it.
    assert response.status_code == 200
    assert response.json()["inserted"] == 1

    # (1) Stored as audio with no text. Asserted BEFORE the transcript lands would be a race,
    # so it is asserted from the payload instead: `_transcription` is only written by the
    # transcriber, and its `duration_estimated` flag proves `text` was NULL when it ran (a
    # captioned message would have been skipped as `already_has_text`).
    wait_for_extraction(db_url, household_id)
    [message] = messages(db_url, household_id)
    assert message.message_type == "audio"
    assert message.provider == "gowa"
    assert message.provider_message_id == provider_id

    # (2) The transcript is on the row and the payload is marked.
    assert message.text == SPOKEN
    assert message.payload[TRANSCRIBED_MARKER] is True
    assert message.payload["_transcription"]["model"] == "gpt-4o-mini-transcribe"
    # The declared 20s was used, not a byte-count guess.
    assert message.payload["_transcription"]["billed_seconds"] == DECLARED_SECONDS
    assert message.payload["_transcription"]["duration_estimated"] is False
    # The provider's own keys survive untouched — the payload is re-read by re-extraction.
    assert message.payload["from_name"] == "Sarah"

    # The bytes really came from the fake sidecar over HTTP, on the documented route.
    assert gowa_server.downloads == [f"/message/{provider_id}/download"]
    # ...and the encrypted mmg.whatsapp.net URL was never fetched: the only request this test's
    # server saw is the one above, and nothing else could have produced the audio.
    [call] = transcriber.calls
    assert call["byte_count"] == len(AUDIO_BYTES)
    assert call["model"] == "gpt-4o-mini-transcribe"
    # The extension is how the API picks a decoder; `audio/ogg; codecs=opus` must survive its
    # parameters.
    assert call["filename"] == "voice-note.ogg"
    assert call["content_type"] == "audio/ogg"

    # (3) One audited run, with a real cost. 20s at $0.003/min = $0.001.
    [run] = runs(db_url, household_id, TRANSCRIBE_PURPOSE)
    assert run.status == "ok"
    assert run.model == "gpt-4o-mini-transcribe"
    assert run.cost_usd > 0
    assert str(run.cost_usd) == "0.001000"
    # It is on the household, which is the only thing the budget guard sums by.
    assert run.household_id == household_id

    # (5) And the model read the words, attributed as spoken.
    prompt = extraction.transcript()
    assert SPOKEN in prompt
    assert "[voice note]" not in prompt
    assert (
        prompt
        == f"[m1] {message.sent_at.astimezone(LONDON):%a %Y-%m-%d %H:%M} — Sarah (voice note): {SPOKEN}"
    )


# --- 4: a replay is free ---------------------------------------------------------------------


@pytest.mark.db
def test_a_gowa_replay_neither_transcribes_nor_charges_again(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
) -> None:
    """GOWA retries a delivery it did not see a 2xx for, up to five times. Every one of those
    is the same audio: charging for it five times would be five times the bill and five
    identical `llm_runs` rows against the household's budget."""
    client = make_client(gowa_url=gowa_server.url)
    body = voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}")

    assert post(client, body).status_code == 200
    wait_for_extraction(db_url, household_id)
    assert len(transcriber.calls) == 1

    # Byte-identical redelivery, exactly as GOWA retries it.
    replay = post(client, body)
    assert replay.status_code == 200
    assert replay.json()["inserted"] == 0

    # Nothing to wait for on a replay — `inserted == 0` means no task is even scheduled — so
    # give any task that might wrongly exist a real chance to run before asserting it did not.
    time.sleep(1.0)
    assert len(transcriber.calls) == 1, "a replay must not reach the transcription API"
    assert len(gowa_server.downloads) == 1, "a replay must not re-download the audio"
    assert len(runs(db_url, household_id, TRANSCRIBE_PURPOSE)) == 1, "a replay must not be billed"
    assert len(messages(db_url, household_id)) == 1


@pytest.mark.db
def test_a_second_transcription_attempt_on_the_same_row_is_refused(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
) -> None:
    """The replay above is free because ingest deduplicates. This proves the SECOND guard —
    the one that holds when something calls the transcriber directly: a backfill, a retried
    task, a future re-extraction. It must be free at the transcription layer too."""
    client = make_client(gowa_url=gowa_server.url)
    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    wait_for_extraction(db_url, household_id)
    [message] = messages(db_url, household_id)

    async def again(session: AsyncSession) -> Any:
        result = await transcription.transcribe_voice_note(session, message.id)
        await session.commit()
        return result

    assert query(db_url, again) is None
    assert len(transcriber.calls) == 1
    assert len(gowa_server.downloads) == 1
    assert len(runs(db_url, household_id, TRANSCRIBE_PURPOSE)) == 1


# --- 6: the untranscribed voice note is unchanged --------------------------------------------


@pytest.mark.db
def test_an_untranscribable_voice_note_still_renders_the_placeholder(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
    extraction: _ExtractionSpy,
) -> None:
    """The media aged out of the sidecar (GOWA 404s). Everything must be exactly as it was
    before this feature existed: `[voice note]`, no crash, no charge."""
    gowa_server.status = 404
    client = make_client(gowa_url=gowa_server.url)

    assert post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}")).status_code == 200
    assert wait_for(lambda: len(gowa_server.downloads) == 1)

    # It tried, it failed, and it did not pay: the 404 body is read for its error code, but no
    # transcription API call was made and no run was recorded.
    assert transcriber.calls == []
    assert runs(db_url, household_id, TRANSCRIBE_PURPOSE) == []

    # The row is untouched, and it is NOT stamped extracted — the grace window is still open,
    # so extraction has not yet been allowed to see it as a placeholder.
    [message] = messages(db_url, household_id)
    assert message.text is None
    assert TRANSCRIBED_MARKER not in message.payload
    assert message.extracted_at is None

    # Past the window it becomes ordinary again. This is the whole degrade path: late, and
    # identical to yesterday's behaviour.
    backdate(db_url, household_id, minutes=30)
    post(client, typed(chat_id, f"gowa-{uuid.uuid4().hex[:10]}", "did she eat anything?"))
    wait_for_extraction(db_url, household_id, count=2)

    prompt = extraction.transcript()
    assert "— Sarah: [voice note]" in prompt
    assert "(voice note)" not in prompt, "nothing was heard, so nothing may be attributed"


# --- 7: the kill switch ----------------------------------------------------------------------


@pytest.mark.db
def test_transcription_disabled_costs_nothing_and_changes_nothing(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
    extraction: _ExtractionSpy,
) -> None:
    """`transcribe_voice_notes=False` is the switch to reach for if the audio bill or the
    sidecar misbehaves, so it has to be believable: not one byte fetched, not one call, and no
    lingering behavioural difference from the day before this feature shipped.

    THE SWITCH HAS TO BE INSTANT. `_awaiting_transcription` hides a voice note while a
    transcript is on its way — but with transcription off, none ever is, and a switch that
    still delayed every voice note by `TRANSCRIPTION_GRACE` would be a surprise waiting for
    whoever flips it at 2am. So the message must extract on the FIRST run, with no backdating
    here to help it.
    """
    client = make_client(gowa_url=gowa_server.url, transcribe_voice_notes=False)

    assert post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}")).status_code == 200
    wait_for_extraction(db_url, household_id)

    assert gowa_server.downloads == [], "disabled must skip BEFORE the download, not after"
    assert transcriber.calls == []
    assert runs(db_url, household_id, TRANSCRIBE_PURPOSE) == []
    [message] = messages(db_url, household_id)
    assert message.text is None
    assert TRANSCRIBED_MARKER not in message.payload
    assert "— Sarah: [voice note]" in extraction.transcript()


# --- 8: the sidecar is down -------------------------------------------------------------------


@pytest.mark.db
def test_a_dead_sidecar_leaves_the_message_alone_and_the_webhook_answers_200(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    dead_port: str,
    transcriber: type[_StubTranscriber],
    extraction: _ExtractionSpy,
) -> None:
    """A non-2xx here is five GOWA retries and five copies of the message in the family's
    chat, so the failure that matters is not "no transcript" — it is a raised exception."""
    client = make_client(gowa_url=dead_port)

    response = post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    assert response.status_code == 200
    assert response.json()["inserted"] == 1

    # Let the connection get refused and the task finish.
    time.sleep(1.5)
    assert transcriber.calls == []
    assert runs(db_url, household_id, TRANSCRIBE_PURPOSE) == []
    [message] = messages(db_url, household_id)
    assert message.text is None
    assert TRANSCRIBED_MARKER not in message.payload

    # And the app carries on: the very next message extracts normally rather than the
    # household being wedged behind a failed download.
    backdate(db_url, household_id, minutes=30)
    assert (
        post(client, typed(chat_id, f"gowa-{uuid.uuid4().hex[:10]}", "on my way")).status_code
        == 200
    )
    wait_for_extraction(db_url, household_id, count=2)
    assert "on my way" in extraction.transcript()
    assert "[voice note]" in extraction.transcript()


# --- the ordering ------------------------------------------------------------------------------


@pytest.mark.db
def test_no_extraction_run_ever_sees_the_placeholder_when_a_transcript_is_coming(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
    extraction: _ExtractionSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SILENT FAILURE. If extraction reads the row first it finds nothing, stamps
    `extracted_at`, and the transcript lands against a message nothing will look at again —
    no exception, no log line, no failed run. Just a fall nobody hears about.

    A second webhook is posted INTO the transcription window on purpose: the `await` in
    `extract_in_background` only orders the task that scheduled the transcription, and this is
    the run it cannot order. `_awaiting_transcription` is what has to hold here.
    """
    slow = asyncio.Event()

    class _SlowTranscriber(_StubTranscriber):
        async def transcribe(self, **kwargs: Any) -> RawTranscription:
            await asyncio.sleep(1.5)
            return await super().transcribe(**kwargs)

    monkeypatch.setattr(transcription, "OpenAITranscriber", _SlowTranscriber)
    assert slow is not None  # keep the name meaningful if the sleep is ever replaced

    client = make_client(gowa_url=gowa_server.url)
    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))

    # Land a second message while the first is still being transcribed. This is the concurrent
    # extraction run that would have read "[voice note]".
    time.sleep(0.4)
    post(client, typed(chat_id, f"gowa-{uuid.uuid4().hex[:10]}", "I'll ring the GP"))

    wait_for_extraction(db_url, household_id, count=2)
    time.sleep(0.5)  # let any straggling run finish before we look at the whole history

    # EVERY run, not just the last: the assertion is that no run ANYWHERE was shown the
    # placeholder, because a run that was shown it has already stamped the row.
    audio_seen = [m for m in extraction.seen if m.message_type == "audio"]
    assert audio_seen, "the voice note was never extracted at all"
    for shown in audio_seen:
        assert shown.text == SPOKEN, (
            "an extraction run read the voice note before its words existed"
        )
        assert shown.transcribed is True

    # The row itself: extracted exactly once, carrying words.
    stored = messages(db_url, household_id)
    assert len(stored) == 2
    voice = next(m for m in stored if m.message_type == "audio")
    assert voice.text == SPOKEN
    assert voice.extracted_at is not None
    assert len(runs(db_url, household_id, TRANSCRIBE_PURPOSE)) == 1


@pytest.mark.db
def test_the_cursor_withholds_an_untranscribed_voice_note_and_then_releases_it(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
) -> None:
    """The ordering rule, stated directly against the query rather than through a race.

    A race test can pass by luck. This asserts the mechanism: the same row, same household,
    read twice, differing only in how old it is.
    """
    gowa_server.status = 404  # never transcribed, so the marker never lands
    client = make_client(gowa_url=gowa_server.url)
    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    assert wait_for(lambda: len(messages(db_url, household_id)) == 1)

    async def pending(session: AsyncSession) -> list[ChunkMessage]:
        return await service._unextracted(session, household_id)

    # Inside the window: invisible, so nothing can extract it as a placeholder.
    assert query(db_url, pending) == []

    # Past it: visible, so it can never be withheld forever.
    backdate(db_url, household_id, minutes=30)
    [released] = query(db_url, pending)
    assert released.message_type == "audio"
    assert released.text is None
    assert released.transcribed is False


@pytest.mark.db
def test_an_imported_audio_line_is_never_withheld_because_nothing_will_transcribe_it(
    db_url: str,
    household_id: uuid.UUID,
    settings_override: Callable[..., Settings],
) -> None:
    """A `<audio omitted>` line from a `.txt` export has no bytes anywhere we can reach.

    `provider="whatsapp_export"` and `provider_message_id IS NULL`, so
    `transcription._skip_reason` refuses it instantly as `no_downloadable_media` — no transcript
    is coming, now or ever. Withholding it is therefore pure loss, and it is not a hypothetical
    loss: an import runs extraction with `force=True` the moment the file lands, so every audio
    line in the export would be skipped by the very run the family is watching a progress bar
    for, left `extracted_at IS NULL`, and missing from that bar's `extracted_count` — under an
    import that then reported itself "complete".

    This is a plain DB test rather than a real upload because the mechanism is one predicate,
    and going through the whole import endpoint would prove it for one export shape only.
    """
    settings_override(env="test", database_url=db_url, test_database_url=db_url)
    imported_id = uuid.uuid4()

    async def add_export_lines(session: AsyncSession) -> None:
        for index, (message_type, text) in enumerate([("text", "hello"), ("audio", None)]):
            session.add(
                Message(
                    id=imported_id if message_type == "audio" else uuid.uuid4(),
                    household_id=household_id,
                    provider="whatsapp_export",
                    provider_message_id=None,  # exports carry no message id at all
                    content_hash=hashlib.sha256(f"{household_id}{index}".encode()).digest(),
                    sender_display_name="Sarah",
                    sent_at=datetime.now(UTC) - timedelta(days=30 - index),
                    source_ordinal=index,
                    message_type=message_type,
                    text=text,
                    payload={},
                )
            )
        await session.commit()

    query(db_url, add_export_lines)

    async def pending(session: AsyncSession) -> list[ChunkMessage]:
        return await service._unextracted(session, household_id)

    # Freshly ingested — inside the grace window — and still visible, because there is nothing
    # to be waiting for.
    seen = query(db_url, pending)
    assert imported_id in {m.id for m in seen}, (
        "an imported audio line was withheld from the import's own extraction run"
    )
    assert "— Sarah: [voice note]" in render_transcript(build_chunk(seen, ()), LONDON)


@pytest.mark.db
def test_the_tick_and_the_cursor_agree_about_which_households_are_due(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
) -> None:
    """`households_due_for_extraction` picks the work list; `_unextracted` is what the run then
    sees. If the first counted a withheld voice note the second will not return, the tick wakes
    a household to extract nothing — a run, a lock and a log line for no reason, forever."""
    gowa_server.status = 404
    client = make_client(gowa_url=gowa_server.url)
    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    assert wait_for(lambda: len(messages(db_url, household_id)) == 1)

    async def due(session: AsyncSession) -> list[uuid.UUID]:
        return await service.households_due_for_extraction(session)

    assert household_id not in query(db_url, due)


def test_the_grace_window_is_longer_than_the_slowest_run_that_could_still_succeed(
    settings_override: Callable[..., Settings],
) -> None:
    """The one part of the ordering that is arithmetic, so it is checked as arithmetic.

    `TRANSCRIPTION_GRACE` withholds a voice note while its transcript is fetched. If a
    transcription can still be in flight after the window shuts, the message becomes visible
    again mid-flight, a concurrent run extracts "[voice note]" and stamps `extracted_at`, and
    the words land seconds later on a row nothing will read — no exception, no failed run, no
    log line. Every stage between `ingested_at` and the marker is separately bounded, so the
    window simply has to be longer than their sum.

    This is asserted against the CONSTANTS rather than a number typed here, because the failure
    mode is drift: someone raising the OpenAI timeout to give reports more room has no reason
    to think about voice notes, and would silently reopen the race. Now they get a failing test
    with this docstring on it.
    """
    settings_override(openai_api_key="sk-test-not-a-real-key")

    commit_wait = webhooks.COMMIT_WAIT_TRIES * webhooks.COMMIT_WAIT_SECONDS
    # `read` on a streamed response is the PER-CHUNK budget, not the whole transfer, so this
    # under-states a pathologically slow sidecar. It is the bound worth checking anyway: a
    # download that stalls chunk after chunk is a broken sidecar, not a slow transcription, and
    # no window can be written that covers it.
    download = (gowa.MEDIA_TIMEOUT.connect or 0) + (gowa.MEDIA_TIMEOUT.read or 0)
    # Ask the client rather than restating its timeout — that is the constant that drifts.
    model_call = get_openai_client().timeout.read or 0

    worst_case = timedelta(seconds=commit_wait + float(download) + float(model_call))
    assert worst_case < service.TRANSCRIPTION_GRACE, (
        f"a transcription can take up to {worst_case} but is only withheld for "
        f"{service.TRANSCRIPTION_GRACE} — extraction can read the placeholder first"
    )


# --- the logs -----------------------------------------------------------------------------------


@pytest.mark.db
def test_not_one_word_of_the_transcript_reaches_the_logs(
    make_client: Callable[..., TestClient],
    db_url: str,
    chat_id: str,
    household_id: uuid.UUID,
    gowa_server: _FakeGowa,
    transcriber: type[_StubTranscriber],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Care conversations are the most sensitive text this product handles, and a transcript is
    the same text with a machine's confidence attached. Ids, durations and byte counts only."""
    caplog.set_level("DEBUG")
    client = make_client(gowa_url=gowa_server.url)
    post(client, voice_note(chat_id, f"gowa-{uuid.uuid4().hex[:10]}"))
    wait_for_extraction(db_url, household_id)

    # The rendered message AND every `extra` field, because the JSON formatter emits both and a
    # transcript smuggled into `extra={"text": ...}` would never appear in `getMessage()`.
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.extend(str(value) for value in record.__dict__.values())
    haystack = "\n".join(parts).lower()
    for word in ("fall", "shaken", "mum had"):
        assert word not in haystack, f"{word!r} from the transcript reached a log record"
