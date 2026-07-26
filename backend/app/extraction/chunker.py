"""Cutting the conversation into chunks the extraction model reads one at a time.

WHY 120 MESSAGES AND NOT 2,000. A family WhatsApp message averages ~40 characters, about
12 tokens; with the line envelope (`[m17] Tue 2026-07-14 21:04 — Sarah: `) that is ~30
tokens, so 120 messages is ~3,600 tokens. Against a 1M-token context window that is
nothing — we are nowhere near any limit, and the chunk size is NOT a context decision.
It is chosen for RECALL and BLAST RADIUS: recall degrades measurably as the haystack
grows, so a long chunk quietly loses events with no error to point at, and when a chunk
does fail, a small one loses a day rather than a month and costs pennies to retry.

The seam is deliberately pure — no DB, no clock, no network. The runner selects rows,
adapts them to `ChunkMessage`, and owns the `messages.extracted_at` cursor; everything
about *where the cuts go* is decided here and is testable with a list of dataclasses.

Every constant below is a module-level name so the eval harness can tune it without
editing code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID
    from zoneinfo import ZoneInfo

# The size the rules below settle on in practice, not a rule itself: a family sending
# ~120 messages a day crosses MIN_FOR_GAP_BREAK during the afternoon and closes on the
# overnight quiet gap, which is where the plan's "1 chunk/day" cost model comes from.
TARGET = 120
HARD_MAX = 200
MIN_FOR_GAP_BREAK = 80
QUIET_GAP = timedelta(hours=4)
OVERLAP_MESSAGES = 20
OVERLAP_MAX_AGE = timedelta(hours=6)
MAX_SPAN = timedelta(days=14)

# Not `%a`: strftime's weekday names follow the process locale, so a French-locale
# laptop would send a different prompt than the container. The prompt must be identical.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# What the model is told was shared. It must know something arrived without being
# invited to hallucinate its contents: "[photo]" alone produces nothing, while
# "here's the discharge letter [document: discharge-letter.pdf]" is a real signal.
_MEDIA_LABELS = {
    "image": "photo",
    "video": "video",
    "voice": "voice note",
    "audio": "voice note",
    "document": "document",
    "sticker": "sticker",
    "contact": "contact card",
    "location": "location",
}

# A TRANSCRIPT IS THE ONE MEDIA WE DO NOT PLACEHOLDER, AND THAT IS NOT A CONTRADICTION.
# The rule above exists because we do not have the photo: "[photo]" is all we honestly know, and
# printing a caption beside it invites the model to describe an image nobody read. A transcript
# is the opposite case — it IS the contents, in the speaker's own words, and hiding it behind
# "[voice note]" throws away the message. In a family care chat that is not a rare shape:
# "Mum had a fall this morning" is spoken far more often than it is typed.
#
# The label stays, moved to where it belongs — beside the SENDER rather than in place of the
# words:
#
#     [m17] Tue 2026-07-14 21:04 — Sarah (voice note): she had a bad night again
#
# so the model weighs it as speech that a machine heard, not as text a human proof-read. That
# matters for exactly the words that matter most: transcription mishears names and drug names,
# and "(voice note)" is the model's only cue that "a pixie band" might have been "Apixaban".
SPOKEN_LABEL = "voice note"


@dataclass(frozen=True, slots=True)
class ChunkMessage:
    """One persisted message, reduced to what chunking and rendering need.

    Deliberately not `InboundMessage`: chunking happens after persistence, so a message
    has an id, and it happens before prompting, so it must not carry a payload blob.
    """

    id: UUID
    sent_at: datetime  # tz-aware; rendered in the household timezone, never stored local
    sender_display_name: str | None
    text: str | None
    message_type: str = "text"
    media_filename: str | None = None  # only for `[document: discharge-letter.pdf]`
    source_ordinal: int | None = None  # export line number; the tiebreak within a minute
    # `text` came from a speech-to-text model, not from a keyboard. Carried explicitly rather
    # than inferred from `message_type == "audio" and text`, because an audio FILE forwarded
    # with a caption has both and is not a transcript — printing that caption as the spoken
    # words would attribute a sentence to a recording nobody transcribed.
    transcribed: bool = False


# The two handle namespaces, defined ONCE. `render_transcript` prints these labels, `handles`
# maps them to ids and the runner resolves the model's citations with them; if the three ever
# disagreed, every event would be dropped as `no_valid_handle` with nothing to point at.
PRIMARY_HANDLE = "m"
CONTEXT_HANDLE = "c"


def handles_for(prefix: str, messages: Sequence[ChunkMessage]) -> dict[str, ChunkMessage]:
    """`{"m1": msg, "m2": msg, ...}` — the one place a handle string is ever formed."""
    return {f"{prefix}{i}": message for i, message in enumerate(messages, 1)}


@dataclass(frozen=True, slots=True)
class Chunk:
    """Primary messages the model may cite, plus context it may only read.

    `handles` is the whole reason this type exists: the transcript labels messages
    `m1..mN` / `c1..cN` and the model cites those labels back, because a UUID is ~18
    tokens and models transpose long hex runs. The runner maps handles to ids with this.
    """

    primary: tuple[ChunkMessage, ...]
    context: tuple[ChunkMessage, ...]
    handles: dict[str, UUID]

    def primary_by_handle(self) -> dict[str, ChunkMessage]:
        """What the runner resolves a citation against — primary only, never context.

        A `context_only` line is readable and uncitable, and that rule is enforced by simply
        not being in this mapping: an event whose sole evidence is context has no valid handle.
        """
        return handles_for(PRIMARY_HANDLE, self.primary)

    def context_handles(self) -> set[str]:
        return set(handles_for(CONTEXT_HANDLE, self.context))


def build_chunks(
    unextracted: Sequence[ChunkMessage],
    *,
    already_extracted: Sequence[ChunkMessage] = (),
) -> list[Chunk]:
    """Split unextracted messages into chunks, prepending context-only overlap.

    A message appears as primary in exactly one chunk — that is what makes
    `messages.extracted_at` a sound cursor. It may also appear as context in the next
    chunk, where it is explicitly forbidden as an event source.
    """
    history = _ordered(already_extracted)
    chunks: list[Chunk] = []
    for group in _split(_ordered(unextracted)):
        chunks.append(build_chunk(group, _context_for(group[0], history)))
        # Only the tail can ever be context again, and keeping the whole history would
        # make a 334-chunk backfill rescan every message it has already chunked.
        history = (history + group)[-OVERLAP_MESSAGES:]
    return chunks


def render_transcript(chunk: Chunk, tz: ZoneInfo) -> str:
    """Render the chunk as the model sees it, context first.

        [c1] context_only Tue 2026-07-14 18:02 — Tom: is she still off her food?
        [m1] Tue 2026-07-14 21:04 — Sarah: she had a bad night again, up 4 times
        [m2] Tue 2026-07-14 21:09 — Tom (voice note): I'll ring the surgery in the morning
        [m3] Tue 2026-07-14 21:11 — Tom: [photo]

    Printing the WEEKDAY is not decoration. Models resolve "last Tuesday" reliably when
    the weekday is on the line and unreliably when they must derive it from an ISO date,
    and every date in this product is anchored by exactly that arithmetic.

    Times are rendered in the household timezone for the same reason the dedup buckets
    are: the family said "last night", and last night is a local-clock fact.
    """
    lines = [
        _render_line(handle, m, tz, context=True)
        for handle, m in handles_for(CONTEXT_HANDLE, chunk.context).items()
    ]
    lines += [
        _render_line(handle, m, tz, context=False)
        for handle, m in chunk.primary_by_handle().items()
    ]
    return "\n".join(lines)


def estimated_chunk_count(message_count: int) -> int:
    """Chunks an import will take, for the cost estimate shown before it runs.

    Uses TARGET rather than simulating the split, because the quote ("38,400 messages,
    about $19") is shown before a single timestamp has been parsed. Real chunks land
    anywhere in MIN_FOR_GAP_BREAK..HARD_MAX depending on how the family talks, so this is
    a mid-range estimate and not a bound — the actual money guard is IMPORT_MAX_SPEND_USD
    aborting mid-flight, which leaves `extracted_at` NULL and resumes.
    """
    return (max(message_count, 0) + TARGET - 1) // TARGET


def _ordered(messages: Sequence[ChunkMessage]) -> list[ChunkMessage]:
    # Export timestamps have no seconds, so many messages share a minute; source_ordinal
    # is the line number that breaks those ties, and the id breaks the rest so the same
    # input always chunks the same way. Messages with no ordinal sort first within a tie.
    return sorted(
        messages,
        key=lambda m: (m.sent_at, -1 if m.source_ordinal is None else m.source_ordinal, str(m.id)),
    )


def _split(messages: list[ChunkMessage]) -> list[list[ChunkMessage]]:
    groups: list[list[ChunkMessage]] = []
    buffer: list[ChunkMessage] = []
    for message in messages:
        if buffer and _closes_before(buffer, message):
            groups.append(buffer)
            buffer = []
        buffer.append(message)
        if len(buffer) == HARD_MAX:
            groups.append(buffer)
            buffer = []
    if buffer:
        groups.append(buffer)
    return groups


def _closes_before(buffer: list[ChunkMessage], message: ChunkMessage) -> bool:
    """Would this message start a new chunk?

    A quiet gap is the boundary we want — nobody's thread spans four silent hours — but
    only once the chunk is worth paying the ~1,400-token prompt overhead for, hence the
    MIN_FOR_GAP_BREAK floor. The span cap catches the opposite case: a nearly dead group
    where 30 messages trickle over two months, and "yesterday" in the last one must not
    be resolved next to a message from March.
    """
    if message.sent_at - buffer[0].sent_at > MAX_SPAN:
        return True
    return len(buffer) >= MIN_FOR_GAP_BREAK and message.sent_at - buffer[-1].sent_at >= QUIET_GAP


def _context_for(first: ChunkMessage, history: list[ChunkMessage]) -> tuple[ChunkMessage, ...]:
    """The tail of what came before, so a cut never orphans a pronoun from its antecedent.

    OVERLAP_MAX_AGE is 6h against a 4h QUIET_GAP on purpose: a chunk closed by the
    minimum quiet gap still gets its context, while one closed by an overnight silence
    gets none — and correctly so, since nothing said before bed disambiguates the morning.
    """
    cutoff = first.sent_at - OVERLAP_MAX_AGE
    return tuple(m for m in history if m.sent_at >= cutoff)[-OVERLAP_MESSAGES:]


def build_chunk(primary: Sequence[ChunkMessage], context: Sequence[ChunkMessage]) -> Chunk:
    """Assemble one chunk. Public because the runner builds chunks too, when it splits one."""
    handles = {h: m.id for h, m in handles_for(CONTEXT_HANDLE, context).items()}
    handles |= {h: m.id for h, m in handles_for(PRIMARY_HANDLE, primary).items()}
    return Chunk(primary=tuple(primary), context=tuple(context), handles=handles)


def _render_line(handle: str, message: ChunkMessage, tz: ZoneInfo, *, context: bool) -> str:
    local = message.sent_at.astimezone(tz)
    stamp = f"{_WEEKDAYS[local.weekday()]} {local:%Y-%m-%d %H:%M}"
    marker = "context_only " if context else ""
    sender = message.sender_display_name or "Unknown"
    if _is_transcript(message):
        sender = f"{sender} ({SPOKEN_LABEL})"
    return f"[{handle}] {marker}{stamp} — {sender}: {_render_body(message)}".rstrip()


def _is_transcript(message: ChunkMessage) -> bool:
    """Words we have, spoken rather than typed. Untranscribed audio is NOT this."""
    return message.transcribed and bool(message.text and message.text.strip())


def _render_body(message: ChunkMessage) -> str:
    # One message is one line: a raw newline would let the model read half a message as
    # a separate, unhandled one and cite a handle that does not cover the words it used.
    text = " ".join(message.text.split()) if message.text else ""
    if message.message_type == "text" or _is_transcript(message):
        # A transcript prints as its words, with no placeholder in front of them. The
        # `(voice note)` on the sender is what keeps it honest — see SPOKEN_LABEL.
        return text
    label = _MEDIA_LABELS.get(message.message_type, "attachment")
    placeholder = f"[{label}: {message.media_filename}]" if message.media_filename else f"[{label}]"
    return f"{placeholder} {text}".rstrip()
