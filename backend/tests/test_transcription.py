"""Voice note transcription, end to end against a real socket and a real database.

THE FAKE GOWA IS A REAL HTTP SERVER. `app.gowa.download_media` streams a binary body, caps it
mid-stream, reads an error body only when the status says error, and self-heals a
`DEVICE_ID_REQUIRED` 400 — none of which is exercised by a mocked transport that hands back a
constructed `httpx.Response`. It serves a REAL (tiny) WAV file, because the thing being proven
is that bytes off a socket reach the transcriber unaltered.

The OpenAI call is the ONLY stub: `StubTranscriber` counts its calls, which is how "a replay
does not pay twice" is a test rather than a hope. No test here touches the network beyond
127.0.0.1 and none of them can spend money.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
import threading
import uuid
import wave
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.config
from app import gowa
from app.config import Settings
from app.db import to_asyncpg_url
from app.llm import pricing
from app.models import Base, Household, LlmRun, Message
from app.transcription import (
    TRANSCRIBED_MARKER,
    TRANSCRIPTION_META_KEY,
    RawTranscription,
    Transcript,
    transcribe_voice_note,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 7, 25, 9, 30, tzinfo=UTC)
PROVIDER_MESSAGE_ID = "3EB0C767D26B8FB1C2A9"
SPOKEN = "Mum had a fall this morning but she's okay, the GP is calling back at four."


def tiny_wav(seconds: float = 1.0) -> bytes:
    """A real audio file: RIFF header, 8 kHz mono, silence. Small enough to inline, real
    enough that nothing downstream is being handed a made-up blob."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buffer.getvalue()


AUDIO = tiny_wav()


# --- the fake sidecar ----------------------------------------------------------------------


class FakeGowa:
    """A real HTTP server on 127.0.0.1 speaking just enough GOWA."""

    def __init__(self) -> None:
        self.media: bytes | None = AUDIO
        self.content_type = "audio/wav"
        self.status = 200
        self.require_device_id = False
        # Chunked responses declare no length, which is what forces the mid-stream cap to be
        # the thing doing the refusing rather than the content-length pre-check.
        self.chunked = False
        self.requests: list[tuple[str, dict[str, str]]] = []
        # Doubles as a fake OpenAI when a test points `openai_base_url` here — see
        # `test_the_real_sdk_path_uploads_the_bytes_and_reads_the_text`.
        self.uploads: list[bytes] = []
        self.transcription_response: dict[str, Any] = {
            "text": SPOKEN,
            "usage": {"type": "duration", "seconds": 12.5},
        }
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep pytest output clean
                return

            def _json(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
                """Stands in for `POST /audio/transcriptions` — no key, no network, no cost."""
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                fake.requests.append((self.path, {}))
                fake.uploads.append(body)
                self._json(200, fake.transcription_response)

            def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
                path, _, query = self.path.partition("?")
                params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
                fake.requests.append((path, params))

                if path == "/devices":
                    self._json(200, {"code": "SUCCESS", "results": [{"id": "dev-1"}]})
                    return
                if not path.endswith("/download"):
                    self._json(404, {"code": "NOT_FOUND"})
                    return
                if fake.require_device_id and "device_id" not in params:
                    self._json(400, {"code": "DEVICE_ID_REQUIRED"})
                    return
                if fake.media is None:
                    self._json(fake.status, {"code": "MEDIA_NOT_FOUND"})
                    return

                self.send_response(200)
                self.send_header("content-type", fake.content_type)
                if fake.chunked:
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for start in range(0, len(fake.media), 1024):
                        piece = fake.media[start : start + 1024]
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    return
                self.send_header("content-length", str(len(fake.media)))
                self.end_headers()
                self.wfile.write(fake.media)

        class QuietServer(ThreadingHTTPServer):
            # The cap test closes the connection mid-body on purpose, which is a broken pipe
            # on this side. Expected, not a failure, and not worth a traceback in the output.
            def handle_error(self, request: Any, client_address: Any) -> None:
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def fake_gowa() -> Iterator[FakeGowa]:
    server = FakeGowa()
    server.start()
    try:
        yield server
    finally:
        server.stop()


class StubTranscriber:
    """The OpenAI seam. Counts calls and remembers the bytes it was handed."""

    def __init__(self, text: str = SPOKEN, duration_seconds: float | None = None) -> None:
        self.text = text
        self.duration_seconds = duration_seconds
        self.calls: list[dict[str, Any]] = []

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, content_type: str
    ) -> RawTranscription:
        self.calls.append(
            {"model": model, "audio": audio, "filename": filename, "content_type": content_type}
        )
        return RawTranscription(text=self.text, duration_seconds=self.duration_seconds)


# --- database ------------------------------------------------------------------------------


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    """Never commits, so a test leaves the database exactly as it found it."""
    engine = create_async_engine(to_asyncpg_url(db_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine) as db:
            yield db
            await db.rollback()
    finally:
        await engine.dispose()


async def make_voice_note(session: AsyncSession, **payload_extra: Any) -> tuple[uuid.UUID, Message]:
    household = Household(
        username=f"fam-{uuid.uuid4().hex[:12]}",
        password_hash="$argon2id$not-a-real-hash",
        name="The Shaws",
        care_recipient_name="Margaret",
    )
    session.add(household)
    await session.flush()
    message = Message(
        household_id=household.id,
        provider="gowa",
        provider_message_id=PROVIDER_MESSAGE_ID,
        content_hash=hashlib.sha256(uuid.uuid4().bytes).digest(),
        sender_display_name="Sarah",
        sent_at=NOW,
        message_type="audio",
        text=None,
        payload={"id": PROVIDER_MESSAGE_ID, "chat_id": "12036@g.us", **payload_extra},
    )
    session.add(message)
    await session.flush()
    return household.id, message


async def stored(session: AsyncSession, message_id: uuid.UUID) -> Message:
    message = await session.get(Message, message_id)
    assert message is not None
    await session.refresh(message)
    return message


async def runs(session: AsyncSession, household_id: uuid.UUID) -> list[LlmRun]:
    result = await session.execute(sa.select(LlmRun).where(LlmRun.household_id == household_id))
    return list(result.scalars())


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, fake_gowa: FakeGowa) -> Callable[..., Settings]:
    """Point the app at the fake sidecar, and let a test change one setting mid-test.

    Not `settings_override`: that binds each module's `get_settings` to a lambda closing over
    one `Settings`, so calling it a second time inside one test finds the modules already
    patched and silently leaves them on the first object. Every module here is patched ONCE, to
    a getter that reads mutable state, so `configured(transcribe_voice_notes=False)` is visible
    to `app.gowa` and `app.transcription` alike.
    """
    state: dict[str, Settings] = {}
    real = app.config.get_settings

    def _apply(**overrides: Any) -> Settings:
        state["settings"] = Settings(
            _env_file=None,
            gowa_url=fake_gowa.url,
            gowa_basic_auth="penny:hunter2",
            openai_api_key="sk-not-used-the-transcriber-is-stubbed",
            **overrides,
        )
        return state["settings"]

    def _current() -> Settings:
        return state["settings"]

    monkeypatch.setattr(app.config, "get_settings", _current)
    for module in list(sys.modules.values()):
        if getattr(module, "get_settings", None) is real:
            monkeypatch.setattr(module, "get_settings", _current)
    _apply()
    return _apply


# --- the happy path -------------------------------------------------------------------------


async def test_a_voice_note_becomes_text_and_is_billed(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    household_id, message = await make_voice_note(session, audio={"duration": 20})
    stub = StubTranscriber()

    transcript = await transcribe_voice_note(session, message.id, transcriber=stub)

    assert isinstance(transcript, Transcript)
    assert transcript.text == SPOKEN
    assert transcript.model == "gpt-4o-mini-transcribe"
    assert transcript.duration_seconds == 20.0
    # 20s of a $0.003/minute model. Pinned exactly: a cost that drifts is a budget that lies.
    assert transcript.cost_usd == Decimal("0.001000")

    # The bytes off the socket are the bytes the transcriber saw, unaltered.
    assert stub.calls[0]["audio"] == AUDIO
    assert stub.calls[0]["audio"][:4] == b"RIFF"
    assert stub.calls[0]["filename"] == "voice-note.wav"

    row = await stored(session, message.id)
    assert row.text == SPOKEN
    # The marker, not a prefix on the text: the words are the family's, the flag is ours.
    assert row.payload[TRANSCRIBED_MARKER] is True
    assert TRANSCRIBED_MARKER not in SPOKEN
    assert row.payload[TRANSCRIPTION_META_KEY]["model"] == "gpt-4o-mini-transcribe"
    assert row.payload[TRANSCRIPTION_META_KEY]["duration_estimated"] is False
    # The provider's payload survives verbatim alongside our two keys.
    assert row.payload["chat_id"] == "12036@g.us"

    audit = await runs(session, household_id)
    assert len(audit) == 1
    assert audit[0].purpose == "transcribe"
    assert audit[0].status == "ok"
    assert audit[0].model == "gpt-4o-mini-transcribe"
    assert audit[0].cost_usd > Decimal("0")
    assert audit[0].cost_usd == transcript.cost_usd


async def test_a_replay_is_free(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """GOWA re-delivers. The second attempt must not download and must not pay."""
    household_id, message = await make_voice_note(session, audio={"duration": 20})
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is not None
    requests_after_first = len(fake_gowa.requests)

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None

    assert len(stub.calls) == 1
    assert len(fake_gowa.requests) == requests_after_first  # nothing was fetched again
    assert len(await runs(session, household_id)) == 1


async def test_the_marker_alone_stops_a_second_charge(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """Belt and braces: even with text cleared, `transcribed` is refusal enough."""
    _, message = await make_voice_note(session, **{TRANSCRIBED_MARKER: True})
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None
    assert stub.calls == []


# --- the refusals ---------------------------------------------------------------------------


async def test_a_forty_minute_podcast_is_refused_before_it_costs_anything(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    household_id, message = await make_voice_note(session, audio={"duration": 2400})
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None

    assert stub.calls == []
    assert fake_gowa.requests == []  # refused before a single byte was pulled
    assert await runs(session, household_id) == []
    assert (await stored(session, message.id)).text is None


async def test_milliseconds_are_not_mistaken_for_a_podcast(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """20,000 "seconds" is 5.5 hours, which no phone recorded. It is 20 seconds in ms."""
    _, message = await make_voice_note(session, audio={"duration": 20000})

    transcript = await transcribe_voice_note(session, message.id, transcriber=StubTranscriber())

    assert transcript is not None
    assert transcript.duration_seconds == 20.0


async def test_gowa_unreachable_leaves_the_voice_note_untouched(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    household_id, message = await make_voice_note(session, audio={"duration": 20})
    fake_gowa.stop()  # the sidecar restarts on every deploy; this is a Tuesday
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None

    assert stub.calls == []
    row = await stored(session, message.id)
    assert row.text is None
    assert TRANSCRIBED_MARKER not in row.payload
    assert await runs(session, household_id) == []
    # And the caller's transaction is still usable, which is what keeps the webhook at 200.
    assert await session.scalar(sa.select(sa.literal(1))) == 1


async def test_media_gone_from_the_sidecar_is_not_an_error(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    _, message = await make_voice_note(session)
    fake_gowa.media = None
    fake_gowa.status = 404

    assert await transcribe_voice_note(session, message.id, transcriber=StubTranscriber()) is None
    assert (await stored(session, message.id)).text is None


async def test_disabled_means_nothing_is_fetched_or_charged(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    _, message = await make_voice_note(session)
    configured(transcribe_voice_notes=False)
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None
    assert stub.calls == []
    assert fake_gowa.requests == []


async def test_an_unpriced_model_refuses_rather_than_spending_unbudgeted(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """The guard that stops a config change spending money the budget cannot see."""
    _, message = await make_voice_note(session)
    configured(transcription_model="gpt-4o-transcribe-diarize")
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, message.id, transcriber=stub) is None
    assert stub.calls == []
    assert fake_gowa.requests == []


async def test_a_photo_and_a_typed_message_are_left_alone(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    _, photo = await make_voice_note(session)
    photo.message_type = "image"
    _, typed = await make_voice_note(session)
    typed.message_type = "text"
    typed.text = "She's fine now"
    await session.flush()
    stub = StubTranscriber()

    assert await transcribe_voice_note(session, photo.id, transcriber=stub) is None
    assert await transcribe_voice_note(session, typed.id, transcriber=stub) is None
    assert stub.calls == []


async def test_a_silent_recording_is_recorded_but_not_marked(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """We paid for the call, so it is audited — but "transcribed" means "has spoken words"."""
    household_id, message = await make_voice_note(session)

    result = await transcribe_voice_note(
        session, message.id, transcriber=StubTranscriber(text="   ")
    )

    assert result is None
    row = await stored(session, message.id)
    assert row.text is None
    assert TRANSCRIBED_MARKER not in row.payload
    audit = await runs(session, household_id)
    assert [run.status for run in audit] == ["empty"]
    assert audit[0].cost_usd > Decimal("0")


async def test_a_missing_message_is_a_none_not_a_crash(
    session: AsyncSession, configured: Callable[..., Settings]
) -> None:
    assert await transcribe_voice_note(session, uuid.uuid4()) is None


# --- billing --------------------------------------------------------------------------------


async def test_an_undeclared_duration_is_still_billed_from_the_bytes(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """No duration anywhere in the payload. A $0 row would make the budget guard a lie."""
    household_id, message = await make_voice_note(session)  # no duration key at all

    transcript = await transcribe_voice_note(session, message.id, transcriber=StubTranscriber())

    assert transcript is not None
    # Unknown, and reported as unknown rather than as the billing estimate.
    assert transcript.duration_seconds is None
    assert transcript.cost_usd > Decimal("0")
    audit = await runs(session, household_id)
    assert audit[0].cost_usd == transcript.cost_usd
    row = await stored(session, message.id)
    assert row.payload[TRANSCRIPTION_META_KEY]["duration_estimated"] is True


async def test_the_api_reported_duration_wins_over_the_payload(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    _, message = await make_voice_note(session, audio={"duration": 20})

    transcript = await transcribe_voice_note(
        session, message.id, transcriber=StubTranscriber(duration_seconds=31.5)
    )

    assert transcript is not None
    assert transcript.duration_seconds == 31.5
    assert transcript.cost_usd == pricing.audio_cost_usd("gpt-4o-mini-transcribe", 31.5)


def test_transcription_is_priced_per_minute_not_per_token() -> None:
    # 60 seconds of the default model is exactly its per-minute rate.
    assert pricing.audio_cost_usd("gpt-4o-mini-transcribe", 60) == Decimal("0.003000")
    assert pricing.audio_cost_usd("gpt-4o-mini-transcribe", 30) == Decimal("0.001500")
    assert pricing.audio_cost_usd("whisper-1", 60) == Decimal("0.006000")
    # The token table and the audio table are separate namespaces, deliberately.
    with pytest.raises(pricing.UnpricedModelError):
        pricing.audio_cost_usd("gpt-5.5-2026-04-23", 60)
    with pytest.raises(pricing.UnpricedModelError):
        pricing.cost_usd("gpt-4o-mini-transcribe", 100, 100)


def test_the_default_transcription_model_has_a_price() -> None:
    """Ships broken otherwise: an unpriced default silently disables every voice note."""
    assert pricing.is_priced_audio_model(Settings(_env_file=None).transcription_model)


def test_the_billing_estimate_errs_towards_over_charging() -> None:
    # 16 kbit/s -> 2,000 bytes per second. A 60 KB clip is billed as 30s, and a real WhatsApp
    # note of that size is shorter, so the estimate never under-charges the household.
    assert pricing.estimate_audio_seconds(60_000) == Decimal("30")
    assert pricing.estimate_audio_seconds(0) == Decimal("0")


# --- the download itself ---------------------------------------------------------------------


async def test_download_media_returns_the_bytes_and_the_content_type(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    fake_gowa.content_type = "audio/ogg; codecs=opus"

    media = await gowa.download_media(PROVIDER_MESSAGE_ID)

    assert media is not None
    audio, content_type = media
    assert audio == AUDIO
    assert content_type == "audio/ogg"  # parameters stripped
    assert fake_gowa.requests[0][0] == f"/message/{PROVIDER_MESSAGE_ID}/download"


async def test_download_media_self_heals_a_device_id_requirement(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """v9 demands a device id on routes the docs do not mention it on. Never guessed: asked."""
    fake_gowa.require_device_id = True

    media = await gowa.download_media(PROVIDER_MESSAGE_ID)

    assert media is not None
    assert media[0] == AUDIO
    paths = [path for path, _ in fake_gowa.requests]
    assert paths == [
        f"/message/{PROVIDER_MESSAGE_ID}/download",  # guess-free first attempt
        "/devices",  # then ask which device
        f"/message/{PROVIDER_MESSAGE_ID}/download",  # then retry
    ]
    assert fake_gowa.requests[-1][1]["device_id"] == "dev-1"


async def test_download_media_refuses_a_body_that_declares_itself_too_big(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    configured(transcription_max_bytes=1024)
    fake_gowa.media = b"\x00" * 4096

    assert await gowa.download_media(PROVIDER_MESSAGE_ID) is None


async def test_download_media_caps_a_body_that_declares_no_length_at_all(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """Chunked, so there is no content-length to check: the cap has to bite mid-stream."""
    configured(transcription_max_bytes=1024)
    fake_gowa.media = b"\x00" * 65536
    fake_gowa.chunked = True

    assert await gowa.download_media(PROVIDER_MESSAGE_ID) is None


async def test_download_media_never_raises_when_the_sidecar_is_gone(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    fake_gowa.stop()
    assert await gowa.download_media(PROVIDER_MESSAGE_ID) is None


async def test_download_media_is_none_when_gowa_is_not_configured(
    settings_override: Callable[..., Any],
) -> None:
    settings_override(gowa_url=None)
    assert await gowa.download_media(PROVIDER_MESSAGE_ID) is None


async def test_a_json_body_is_not_audio(
    fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """A 200 carrying JSON is GOWA talking, not a voice note. Sending it would pay for junk."""
    fake_gowa.media = b'{"code":"SUCCESS","results":{"path":"/statics/media/x.ogg"}}'
    fake_gowa.content_type = "application/json"

    assert await gowa.download_media(PROVIDER_MESSAGE_ID) is None


async def test_nothing_that_was_said_reaches_a_log_line(
    session: AsyncSession,
    fake_gowa: FakeGowa,
    configured: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transcript in a log line is a family's health record in a log aggregator."""
    _, message = await make_voice_note(session, audio={"duration": 20})

    with caplog.at_level(logging.DEBUG):
        assert await transcribe_voice_note(session, message.id, transcriber=StubTranscriber())

    emitted = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [str(record.__dict__) for record in caplog.records]
    )
    assert emitted  # the run really did log something, so this is not vacuous
    assert SPOKEN not in emitted
    for word in ("fall", "Mum", "GP"):
        assert word not in emitted
    # What IS logged: handles and measurements.
    assert "char_count" in emitted


async def test_the_real_sdk_path_uploads_the_bytes_and_reads_the_text(
    session: AsyncSession, fake_gowa: FakeGowa, configured: Callable[..., Settings]
) -> None:
    """`OpenAITranscriber` itself, over real HTTP, against a fake OpenAI on 127.0.0.1.

    Every other test stubs the transcriber, which means the multipart upload, the filename the
    API decodes by, and the response parse are otherwise unproven. Nothing here is billable:
    `openai_base_url` points at the fake, so the real API is never addressed.
    """
    configured(openai_base_url=fake_gowa.url)
    _, message = await make_voice_note(session, audio={"duration": 20})

    transcript = await transcribe_voice_note(session, message.id)

    assert transcript is not None
    assert transcript.text == SPOKEN
    # The API's own measurement wins over the payload's 20 seconds.
    assert transcript.duration_seconds == 12.5
    assert transcript.cost_usd == pricing.audio_cost_usd("gpt-4o-mini-transcribe", 12.5)

    upload = fake_gowa.uploads[0]
    assert AUDIO in upload  # the real bytes, in the multipart body
    assert b'filename="voice-note.wav"' in upload  # the extension the decoder keys on
    assert b"gpt-4o-mini-transcribe" in upload


def test_the_payload_marker_is_the_frozen_key_both_agents_code_against() -> None:
    """`app.transcription.TRANSCRIBED_MARKER` is read by the webhook, the chunker and the feed.

    Renaming it silently makes every already-transcribed voice note look untranscribed — which
    means paying to transcribe it again and rendering it as typed text in the feed.
    """
    assert TRANSCRIBED_MARKER == "transcribed"
