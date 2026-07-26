"""Voice notes, turned into ordinary text so extraction can see them.

In a family care chat — especially one with older relatives in it — "Mum had a fall this
morning" is very often SPOKEN. Before this module those messages reached the timeline as the
placeholder "[voice note]" and nothing else, so a real share of the care signal was invisible:
the feed showed a grey stub and the extraction prompt was told a voice note existed and
deliberately told nothing about its contents.

THE FOUR RULES THIS MODULE IS BUILT AROUND

1. **It never raises.** `transcribe_voice_note` returns `None` for every failure, and `None`
   means "leave it as a voice note". Its caller is the GOWA webhook, which has to answer 2xx:
   a non-2xx makes GOWA retry the same delivery five times and the family gets duplicates. So
   a dead sidecar, a deleted media file, a transcription error and a disabled setting all look
   identical from outside — the message keeps its placeholder and the app carries on.

2. **Every call is audited with a real cost.** Transcription bills per MINUTE OF AUDIO, not per
   token, so it goes through `pricing.audio_cost_usd` and writes its own `llm_runs` row with
   `purpose="transcribe"`. Without that row the per-household monthly budget guard — which is
   just a SUM over `llm_runs.cost_usd` — would silently stop being true the moment voice notes
   started costing money.

3. **Nothing here logs text.** Not the transcript, not the prompt, not a snippet. Ids,
   durations, byte counts and costs only. Every log call below obeys that; keep it that way.

4. **A replay is free.** GOWA re-delivers, tasks get retried, backfills get run twice. The
   payload marker (`TRANSCRIBED_MARKER`) plus `messages.text` already being set are what make
   the second attempt return `None` before it downloads or pays for anything.

WHY THE MARKER, AND NOT A PREFIX ON THE TEXT. A transcript is less reliable than typed text —
names and drug names are exactly what speech-to-text gets wrong ("Amlodipine" and "amyloid",
"Aziz" and "a sees"). Extraction should weigh it accordingly and a family reading the feed
should be able to tell where the words came from. Both of those need a flag, not a decoration:
mangling the text ("[voice] she fell") would put our editorial into the content hash, the
prompt, the search index and the report. `payload["transcribed"] is True` says the same thing
where a reader can act on it and a writer cannot trip over it.

THE PAYLOAD IS THE PROVIDER'S, WITH TWO KEYS ADDED. `messages.payload` is stored verbatim so
re-extraction can read it; this module adds `transcribed` and the `_transcription` metadata
block (model, billed duration) and touches nothing else. The metadata never contains the
transcript.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa

from app import gowa
from app.config import get_settings
from app.llm.pricing import (
    UnpricedModelError,
    audio_cost_usd,
    estimate_audio_seconds,
    is_priced_audio_model,
)
from app.models import LlmRun, Message
from app.openai_client import get_openai_client

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# The payload key. True once the message carries a transcript instead of a placeholder — read
# it to know that these words were SPOKEN, not typed, and to know not to pay for them twice.
TRANSCRIBED_MARKER = "transcribed"
# Sibling block holding how the text was produced. Underscore-prefixed like `_device_id`, so it
# is visibly ours rather than something GOWA sent. NEVER contains the transcript.
TRANSCRIPTION_META_KEY = "_transcription"

# `llm_runs.purpose` is free text on purpose ("the gateway owns this vocabulary and adding a
# value must not need a migration"), and this is not a gateway call at all — the Responses API
# is nowhere near this path — so the row is written here directly under its own name.
TRANSCRIBE_PURPOSE = "transcribe"

# What the webhook's MEDIA_FIELDS normalises voice notes to, plus the raw WhatsApp spellings a
# `.txt` import or a future GOWA field name could produce.
AUDIO_MESSAGE_TYPES = frozenset({"audio", "voice", "ptt"})

# Only GOWA-delivered messages have media to fetch. A `.txt` export line that said "audio
# omitted" refers to a file that was never uploaded anywhere we can reach.
DOWNLOADABLE_PROVIDER = "gowa"

# The upload needs a filename with an extension OpenAI recognises; the bytes alone are not
# enough. WhatsApp voice notes are Opus in an Ogg container, which is why that is the fallback
# for a sidecar that declares nothing useful.
_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}
_DEFAULT_EXTENSION = "ogg"

# Where a duration might be hiding on a GOWA message payload. Nothing about the shape is
# guaranteed — v9 emits media as either a bare string or an object depending on caption — so
# this looks in the obvious places and treats "not found" as ordinary rather than as an error.
_DURATION_KEYS = ("duration", "seconds", "duration_seconds", "length")
_MEDIA_KEYS = ("audio", "voice", "ptt")
# A "duration" larger than this is milliseconds, not seconds: 6,000 seconds is 100 minutes of
# WhatsApp voice note (the app itself caps recordings far below that), while 6,000 ms is 6
# seconds, which is an extremely ordinary voice note.
_MS_THRESHOLD = 6_000


@dataclass(frozen=True, slots=True)
class Transcript:
    """What a voice note turned out to say, and what that cost.

    `duration_seconds` is the clip's REAL length or None — it is not the estimate used for
    billing when the payload declared nothing, because a made-up length that reads like a
    measurement is how a wrong number ends up on a screen.
    """

    text: str
    model: str
    duration_seconds: float | None
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class RawTranscription:
    """The transcriber's answer, before we decide what it costs or where it goes."""

    text: str
    duration_seconds: float | None = None


class AudioTranscriber(Protocol):
    """The no-network seam, same shape as `app.llm.transport.LLMTransport`: one method, and
    everything above it is ours and is tested offline."""

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, content_type: str
    ) -> RawTranscription: ...


class OpenAITranscriber:
    """`audio.transcriptions`, NOT the Responses API.

    This is a plain speech-to-text call: no JSON schema, no reasoning effort, no structured
    output. Forcing it through `app.llm.gateway` would mean inventing a JSON schema for what is
    already a string, and recording token counts this endpoint does not report in the shape the
    gateway understands.

    `response_format="json"` is explicit rather than defaulted: the SDK logs a warning when it
    has to infer the response type, and the format decides which model class comes back.
    `verbose_json` is the only format carrying a `duration`, and it is whisper-1-only — so the
    duration usually comes from `usage` (below) or from the payload, and may be neither.
    """

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, content_type: str
    ) -> RawTranscription:
        client = get_openai_client()
        result = await client.audio.transcriptions.create(
            model=model,
            # (filename, bytes, content_type). The FILENAME'S EXTENSION is how the API decides
            # which decoder to use — bytes with no name are rejected, whatever they contain.
            file=(filename, audio, content_type),
            response_format="json",
        )
        return RawTranscription(text=result.text or "", duration_seconds=_reported_seconds(result))


def _reported_seconds(result: Any) -> float | None:
    """The clip's length as the API measured it, when it says.

    `usage` is a discriminated union. `type="duration"` carries `seconds` and is what the
    duration-billed models (whisper-1) return — an exact measurement, better than anything the
    payload or a byte count can offer. `type="tokens"` is what the gpt-4o transcribe family
    returns instead, and audio tokens are NOT convertible to seconds here, so that case is
    deliberately "unknown" rather than a conversion factor invented to fill the field.
    """
    usage = getattr(result, "usage", None)
    seconds = getattr(usage, "seconds", None) if usage is not None else None
    if isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds > 0:
        return float(seconds)
    # whisper-1 + verbose_json puts it here instead.
    duration = getattr(result, "duration", None)
    if isinstance(duration, int | float) and not isinstance(duration, bool) and duration > 0:
        return float(duration)
    return None


async def transcribe_voice_note(
    session: AsyncSession,
    message_id: UUID,
    *,
    transcriber: AudioTranscriber | None = None,
) -> Transcript | None:
    """Fetch a voice note from GOWA, transcribe it, and write the text onto the message.

    Returns the `Transcript` on success and `None` for every "leave it as a voice note" case:
    the message is not audio, it already has text, it is already marked transcribed,
    transcription is disabled, the clip is longer than `transcription_max_seconds`, the model
    has no price, GOWA is unreachable, the media is gone, or the transcript came back empty.

    NEVER RAISES and NEVER COMMITS. The caller's session owns the transaction; the writes here
    happen inside a SAVEPOINT so that a database failure on this optional enrichment cannot
    poison the transaction the webhook still has to commit.

    `transcriber` exists for tests. Production passes nothing.
    """
    try:
        return await _transcribe(session, message_id, transcriber or OpenAITranscriber())
    except Exception as exc:
        # The bare catch IS the contract (rule 1): a transcription problem must never become a
        # non-2xx on the webhook, because that is five GOWA retries and duplicate messages.
        log.warning(
            "transcribe.failed",
            extra={"message_id": str(message_id), "exc_type": type(exc).__name__},
        )
        return None


async def _transcribe(
    session: AsyncSession, message_id: UUID, transcriber: AudioTranscriber
) -> Transcript | None:
    row = (
        await session.execute(
            sa.select(
                Message.household_id,
                Message.provider,
                Message.provider_message_id,
                Message.message_type,
                Message.text,
                Message.payload,
            ).where(Message.id == message_id)
        )
    ).one_or_none()
    if row is None:
        log.info("transcribe.skipped", extra={"message_id": str(message_id), "why": "no_message"})
        return None

    payload: dict[str, Any] = row.payload or {}

    # (a) The free exits, all of them before anything is downloaded or paid for. `text` being
    # set covers both a captioned media message and a transcript we already wrote; the marker
    # covers a transcript that came back empty-ish but was still charged for.
    why = _skip_reason(row, payload)
    if why is not None:
        log.info("transcribe.skipped", extra={"message_id": str(message_id), "why": why})
        return None

    settings = get_settings()
    model = settings.transcription_model
    if not is_priced_audio_model(model):
        # Loud, because this is a configuration mistake that would otherwise spend money the
        # budget guard could not see. Refusing costs a family their voice notes; guessing a
        # price costs them the guard.
        log.error("transcribe.unpriced_model", extra={"model": model})
        return None

    # (b) The length gate. A 40-minute file in a family chat is somebody forwarding a podcast,
    # not care signal, and it is the shape that quietly costs real money.
    declared_seconds = _payload_duration_seconds(payload)
    if declared_seconds is not None and declared_seconds > settings.transcription_max_seconds:
        log.info(
            "transcribe.too_long",
            extra={
                "message_id": str(message_id),
                "duration_seconds": declared_seconds,
                "max_seconds": settings.transcription_max_seconds,
            },
        )
        return None

    # (c) The bytes. GOWA holds the mediaKey, so this is the only path to them.
    media = await gowa.download_media(row.provider_message_id)
    if media is None:
        log.info(
            "transcribe.no_media", extra={"message_id": str(message_id), "why": "gowa_unavailable"}
        )
        return None
    audio, content_type = media

    # (d) The one paid call.
    started = datetime.now(UTC)
    result = await transcriber.transcribe(
        model=model,
        audio=audio,
        filename=f"voice-note.{_extension(content_type)}",
        content_type=content_type,
    )
    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    # Billed on the best duration available: the API's if it reported one, else the payload's,
    # else an over-estimate inferred from the byte count. Never 0 — a $0 row is a call the
    # budget guard cannot see.
    known_seconds = (
        result.duration_seconds if result.duration_seconds is not None else (declared_seconds)
    )
    billed_seconds = (
        Decimal(str(known_seconds))
        if known_seconds is not None
        else estimate_audio_seconds(len(audio))
    )
    try:
        cost = audio_cost_usd(model, billed_seconds)
    except UnpricedModelError:
        # Unreachable via `is_priced_audio_model` above, and still handled: the call has
        # already happened, so the row must be written rather than the spend disappearing.
        log.error("transcribe.unpriced_model", extra={"model": model})
        cost = Decimal("0")

    text = (result.text or "").strip()

    # (e) The writes, in the caller's transaction, inside a savepoint.
    async with session.begin_nested():
        await _record_run(
            session,
            household_id=row.household_id,
            provider_message_id=row.provider_message_id,
            model=model,
            status="ok" if text else "empty",
            cost=cost,
            latency_ms=latency_ms,
            byte_count=len(audio),
        )
        if not text:
            # Silence, or an audio file with no speech in it. We pay for the call either way,
            # which is why the row above is written first — but the marker is NOT set, because
            # the marker's meaning is "this message carries spoken words".
            log.info("transcribe.empty", extra={"message_id": str(message_id)})
            return None

        updated = await session.execute(
            sa.update(Message)
            # `text IS NULL` makes a concurrent second attempt a no-op instead of a
            # last-writer-wins overwrite of the first transcript.
            .where(Message.id == message_id, Message.text.is_(None))
            .values(
                text=text,
                payload={
                    **payload,
                    TRANSCRIBED_MARKER: True,
                    TRANSCRIPTION_META_KEY: {
                        "model": model,
                        "billed_seconds": float(billed_seconds),
                        "duration_seconds": known_seconds,
                        "duration_estimated": known_seconds is None,
                    },
                },
            )
        )

    if updated.rowcount == 0:
        # Someone else transcribed it between the SELECT and the UPDATE. Their text stands;
        # our cost is still recorded above, because we still spent it.
        log.info("transcribe.raced", extra={"message_id": str(message_id)})
        return None

    log.info(
        "transcribe.ok",
        extra={
            "message_id": str(message_id),
            "model": model,
            "byte_count": len(audio),
            "billed_seconds": float(billed_seconds),
            "char_count": len(text),
            "cost_usd": str(cost),
            "latency_ms": latency_ms,
        },
    )
    return Transcript(
        text=text,
        model=model,
        duration_seconds=float(known_seconds) if known_seconds is not None else None,
        cost_usd=cost,
    )


def _skip_reason(row: Any, payload: dict[str, Any]) -> str | None:
    """Why this message is not a voice note we should be paying to transcribe, or None."""
    if not get_settings().transcribe_voice_notes:
        return "disabled"
    if (row.message_type or "").lower() not in AUDIO_MESSAGE_TYPES:
        return "not_audio"
    if row.text:
        return "already_has_text"
    if payload.get(TRANSCRIBED_MARKER):
        return "already_transcribed"
    if row.provider != DOWNLOADABLE_PROVIDER or not row.provider_message_id:
        return "no_downloadable_media"
    return None


async def _record_run(
    session: AsyncSession,
    *,
    household_id: UUID,
    provider_message_id: str,
    model: str,
    status: str,
    cost: Decimal,
    latency_ms: int,
    byte_count: int,
) -> None:
    """One `llm_runs` row per paid transcription, so the budget guard stays true.

    `input_tokens` and friends stay 0 and that is not a gap: this endpoint reports no token
    usage, and `cost_usd` — the only column the budget sums — is the real number.
    """
    await session.execute(
        sa.insert(LlmRun).values(
            household_id=household_id,
            purpose=TRANSCRIBE_PURPOSE,
            status=status,
            model=model,
            # Identifies a repeated transcription of the same audio without storing anything
            # about it, exactly as the gateway's prompt fingerprint does.
            request_fingerprint=hashlib.sha256(provider_message_id.encode()).hexdigest()[:32],
            attempts=1,
            cost_usd=cost,
            latency_ms=latency_ms,
            error=None if status == "ok" else "empty_transcript",
            finished_at=datetime.now(UTC),
        )
    )
    log.info(
        "transcribe.recorded",
        extra={"model": model, "status": status, "cost_usd": str(cost), "byte_count": byte_count},
    )


def _extension(content_type: str) -> str:
    return _EXTENSIONS.get(content_type.lower(), _DEFAULT_EXTENSION)


def _payload_duration_seconds(payload: dict[str, Any]) -> float | None:
    """The clip's length as GOWA declared it, in seconds, or None if it did not.

    None is a normal answer, not a failure: it means the length gate cannot fire and the byte
    cap in `gowa.download_media` is the only bound left. That is the deliberate trade — a
    payload shape we have not seen must not cost a family their voice notes.
    """
    for value in _duration_candidates(payload):
        seconds = _as_seconds(value)
        if seconds is not None:
            return seconds
    return None


def _duration_candidates(payload: dict[str, Any]) -> list[Any]:
    candidates = [payload.get(key) for key in _DURATION_KEYS]
    for media_key in _MEDIA_KEYS:
        media = payload.get(media_key)
        if isinstance(media, dict):
            candidates.extend(media.get(key) for key in _DURATION_KEYS)
    return candidates


def _as_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    seconds = float(value)
    if seconds <= 0:
        return None
    # WhatsApp sends seconds; some clients send milliseconds. Treating one as the other is
    # either a podcast that slips through the gate or a 6-second note refused as 100 minutes.
    return seconds / 1000.0 if seconds > _MS_THRESHOLD else seconds
