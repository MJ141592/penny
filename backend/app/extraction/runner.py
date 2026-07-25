"""The extraction run: messages in, deduplicated events out. NO DATABASE, deliberately.

In M1 this module has no session, no models and no commit. It returns *what happened* — the
events, the per-chunk stats, the money, and the ids of the messages it successfully processed —
and the caller persists it. That inversion is what lets the whole riskiest part of the product
be exercised by a CLI over a text file before a schema exists, and it is what M4 will keep:
`messages.extracted_at` is stamped from `ExtractionRunResult.extracted_message_ids`, and that
one column is the entire incremental-extraction mechanism.

THREE THINGS THIS FILE IS CAREFUL ABOUT

1. **Unknown handles are dropped, never fatal.** A model that cites `m47` in a 30-message chunk
   has hallucinated a source; the event goes, the run continues, and the RATE is reported.
   A rising invalid-handle rate is the earliest available signal of prompt rot — earlier than
   recall, which needs labelled data to measure.
2. **The spend ceiling is hard and aborts mid-run.** Remaining messages are simply left out of
   `extracted_message_ids`, so the next run picks them up. Resumability is a consequence of the
   cursor, not a mechanism of its own.
3. **A chunk that will not fit halves itself.** `ChunkTooLargeError` means the gateway already
   doubled the output budget and the response was still truncated, so the only lever left is
   less input. Below MIN_CHUNK_MESSAGES we stop and record the failure rather than splitting
   into chunks too small to pay for their own prompt overhead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.extraction.chunker import (
    OVERLAP_MESSAGES,
    Chunk,
    ChunkMessage,
    build_chunk,
    build_chunks,
    render_transcript,
)
from app.extraction.dedup import compute_dedup_key
from app.extraction.merge import merge_events
from app.llm.gateway import CallSpec, ChunkTooLargeError, ContentFilteredError, LLMGateway
from app.llm.prompts import EXTRACT_PROMPT, EXTRACT_PROMPT_VERSION
from app.llm.schemas import ExtractedEvent, ExtractionResult, validate_against_transcript

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from app.ingest.contract import InboundMessage

log = logging.getLogger(__name__)

# The plan's extraction budget. `low` because extraction is span-finding plus light date
# arithmetic; `medium` roughly doubles reasoning tokens, billed at the $30/M output rate.
EXTRACT_MAX_OUTPUT_TOKENS = 8_000
EXTRACT_EFFORT = "low"

# Below this a chunk is not worth splitting again: the ~1,400-token fixed prompt overhead
# would dominate, and a chunk this small failing twice is a bad chunk, not a big one.
MIN_CHUNK_MESSAGES = 30

# Chunks run in ordered batches of this size. The gateway holds its own Semaphore(4) so this
# does not control the request rate — it controls the DIGEST: every chunk in a batch sees the
# events extracted by every earlier batch, which is how cross-chunk duplicates collapse.
MAX_CONCURRENT_CHUNKS = 4

# The `<open_events>` block, capped: it exists so the model can attach an outcome to an
# appointment it is not currently reading, not to re-send the whole history every call.
OPEN_EVENTS_LIMIT = 10

# A date this far outside the transcript was not resolved from it. Dropping beats storing:
# an event dated 2019 sits at the bottom of the feed forever and nobody goes looking for it.
DATE_SANITY_WINDOW = timedelta(days=400)

# Stable ids for messages that have never been persisted, so the CLI and the eval can talk
# about "message m17" across runs. M4 passes real `messages.id` values and never calls this.
_LOCAL_ID_NAMESPACE = uuid5(NAMESPACE_URL, "https://penny.local/extraction/local-message")

DropReason = Literal[
    "no_valid_handle", "context_only_source", "invented_quotes", "out_of_range_date"
]
ChunkStatus = Literal["ok", "filtered", "failed", "skipped"]

# The LLM's spelling on the left, the contract's on the right. The direction matters: the
# contract's vocabulary is the one `events.occurred_at_precision` has a CHECK constraint on,
# so it is the one everything must converge to.
_PRECISION_ALIASES = {"datetime": "exact", "date": "day"}


def normalise_precision(precision: str | None) -> str:
    """One spelling for the two that exist in the repo, and it is the CONTRACT's.

    `app.llm.schemas.Precision` says `datetime`/`date`; `docs/api-contract.md` and
    `models.OCCURRED_AT_PRECISIONS` say `exact`/`day` for the same five values. Everything
    date-shaped goes through here, and it always comes out in the spelling the database will
    accept — normalising the other way round produces a value that fails the CHECK constraint
    the moment M4 persists it.
    """
    if not precision:
        return "unknown"
    return _PRECISION_ALIASES.get(precision, precision)


@dataclass(frozen=True, slots=True)
class OpenEvent:
    """One line of the `<open_events>` block: an event the model may attach an outcome to."""

    kind: str
    occurred_at: str  # already rendered, e.g. "2026-07-22" — never a UUID, never a handle
    title: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    """Verbatim evidence, resolved to a real message. This is what the family is shown."""

    message_id: UUID
    sent_at: datetime
    sender: str | None
    quote: str


@dataclass(slots=True)
class ExtractedEventRecord:
    """One deduplicated event and everything the caller needs to persist it."""

    dedup_key: str
    event: ExtractedEvent
    source_message_ids: list[UUID]
    source_excerpts: list[SourceExcerpt]
    occurred_at_utc: datetime | None
    mention_count: int = 1
    chunk_labels: list[str] = field(default_factory=list)
    merged_by_llm: bool = False

    @property
    def occurred_at_precision(self) -> str:
        """The precision spelled the way `events.occurred_at_precision` will accept it.

        `self.event.occurred_at_precision` is the model's own vocabulary and says `datetime`
        or `date`, neither of which passes the table's CHECK constraint. Persist this one.
        """
        return normalise_precision(self.event.occurred_at_precision)


@dataclass(slots=True)
class ChunkStat:
    """One gateway call, and what it bought. Written for failures too."""

    label: str
    primary_count: int
    context_count: int
    status: ChunkStatus
    events: int = 0
    dropped: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    attempts: int = 0
    latency_ms: int = 0
    error: str | None = None


@dataclass(slots=True)
class ExtractionRunResult:
    """Everything that happened, so the caller can persist it and the eval can score it."""

    events: list[ExtractedEventRecord] = field(default_factory=list)
    chunks: list[ChunkStat] = field(default_factory=list)
    extracted_message_ids: list[UUID] = field(default_factory=list)
    unextracted_message_ids: list[UUID] = field(default_factory=list)
    dropped_events: dict[str, int] = field(default_factory=dict)
    handles_cited: int = 0
    invalid_handles: int = 0
    merge_calls: int = 0
    total_cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    duration_s: float = 0.0
    aborted_reason: str | None = None

    @property
    def invalid_handle_rate(self) -> float:
        """The prompt-rot canary. Zero cited handles is 0.0, not a division by zero."""
        return self.invalid_handles / self.handles_cited if self.handles_cited else 0.0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def local_message_id(message: InboundMessage) -> UUID:
    """A deterministic stand-in for `messages.id` when nothing has been persisted.

    Derived from the message's own content, so the same export yields the same ids on every
    run — which is what lets a recorded eval replay and lets the eval scorer point a
    ground-truth line number at the event that cited it.
    """
    key = "|".join(
        [
            message.provider,
            str(message.source_ordinal),
            message.sent_at.isoformat(),
            message.sender_display_name or "",
            message.text or "",
        ]
    )
    return uuid5(_LOCAL_ID_NAMESPACE, key)


def as_chunk_messages(messages: Sequence[InboundMessage]) -> list[ChunkMessage]:
    """Adapt parsed export messages for a run with no database behind it.

    `InboundMessage` has no id because it does not belong to a household yet, and the chunker
    needs one to build handles. A `uuid5` over the message's own content is deterministic, so
    the same export produces the same ids on every run and a recorded eval replays exactly.

    System lines (joins, subject changes, the encryption notice) are dropped here: they are
    group events, not conversation, and paying to have a model read them finds nothing.
    """
    adapted: list[ChunkMessage] = []
    for message in messages:
        if message.message_type == "system":
            continue
        adapted.append(
            ChunkMessage(
                id=local_message_id(message),
                sent_at=message.sent_at,
                sender_display_name=message.sender_display_name,
                text=message.text,
                message_type=message.message_type,
                media_filename=message.payload.get("filename"),
                source_ordinal=message.source_ordinal,
            )
        )
    return adapted


def occurred_at_utc(occurred_at: str | None, precision: str, tz: ZoneInfo) -> datetime | None:
    """The instant the feed sorts by, encoding precision rather than faking a clock.

    A `day`-precision event is that date at midnight UTC and is NOT timezone-converted: local
    midnight in BST is 23:00 the previous day, which would file "Tuesday" under Monday. Only
    `exact` events — where a time was actually stated — go through the timezone.
    """
    if not occurred_at:
        return None
    grain = normalise_precision(precision)
    text = occurred_at.strip().replace("Z", "+00:00")
    if len(text) == 7 and text[4] == "-":  # "2026-08" from a month-precision answer
        text = f"{text}-01"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None

    if grain == "exact":
        local = moment if moment.tzinfo else moment.replace(tzinfo=tz)
        return local.astimezone(UTC)

    day = moment.date()
    if grain == "week":
        day = day.fromordinal(day.toordinal() - day.weekday())  # Monday of the ISO week
    elif grain == "month":
        day = day.replace(day=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def extract_messages(
    messages: Sequence[ChunkMessage],
    *,
    care_brief: str,
    tz: ZoneInfo,
    gateway: LLMGateway,
    open_events: Sequence[OpenEvent] = (),
    max_spend_usd: Decimal | None = None,
    household_id: UUID | None = None,
    already_extracted: Sequence[ChunkMessage] = (),
) -> ExtractionRunResult:
    """Chunk, extract, dedupe, merge. Returns what happened; persists nothing.

    Takes `ChunkMessage` rather than `InboundMessage` because a message must have an id before
    it can be cited, and `extracted_message_ids` is only useful to M4 if those ids are the real
    `messages.id`. Callers with no database (the CLI, the eval) run their parsed export through
    `as_chunk_messages` first.
    """
    return await _Run(
        messages=messages,
        care_brief=care_brief,
        tz=tz,
        gateway=gateway,
        open_events=tuple(open_events),
        max_spend_usd=max_spend_usd,
        household_id=household_id,
        already_extracted=already_extracted,
    ).execute()


class _Run:
    """One `extract_messages` call. A class because the digest, the spend and the dedup table
    are all state threaded through every chunk, and passing five accumulators around by hand
    is how one of them quietly stops being updated."""

    def __init__(
        self,
        *,
        messages: Sequence[ChunkMessage],
        care_brief: str,
        tz: ZoneInfo,
        gateway: LLMGateway,
        open_events: tuple[OpenEvent, ...],
        max_spend_usd: Decimal | None,
        household_id: UUID | None,
        already_extracted: Sequence[ChunkMessage],
    ) -> None:
        self.messages = messages
        self.care_brief = care_brief.strip()
        self.tz = tz
        self.gateway = gateway
        self.open_events = open_events
        self.max_spend_usd = max_spend_usd
        self.household_id = household_id
        self.already_extracted = already_extracted
        self.result = ExtractionRunResult()
        self.by_key: dict[str, ExtractedEventRecord] = {}
        self._window = _date_window(messages)

    async def execute(self) -> ExtractionRunResult:
        started = time.monotonic()
        # Once per RUN, not per call: a per-call check costs a query per chunk and still
        # cannot stop the call already in flight.
        await self.gateway.check_budget(self.household_id, purpose="extract")

        chunks = build_chunks(self.messages, already_extracted=self.already_extracted)
        for batch_start in range(0, len(chunks), MAX_CONCURRENT_CHUNKS):
            batch = chunks[batch_start : batch_start + MAX_CONCURRENT_CHUNKS]
            if self._over_budget():
                self._abandon(chunks[batch_start:], "spend_cap")
                break
            labels = [str(batch_start + offset + 1) for offset in range(len(batch))]
            outcomes = await asyncio.gather(
                *(
                    self._extract_chunk(chunk, label)
                    for chunk, label in zip(batch, labels, strict=True)
                )
            )
            for chunk, outcome in zip(batch, outcomes, strict=True):
                await self._absorb(chunk, outcome)

        self.result.events = sorted(
            self.by_key.values(),
            key=lambda record: (
                record.occurred_at_utc or datetime.max.replace(tzinfo=UTC),
                record.dedup_key,
            ),
        )
        self.result.duration_s = time.monotonic() - started
        log.info(
            "extraction run chunks=%d events=%d cost=%s invalid_handle_rate=%.3f aborted=%s",
            len(self.result.chunks),
            len(self.result.events),
            self.result.total_cost_usd,
            self.result.invalid_handle_rate,
            self.result.aborted_reason,
        )
        return self.result

    # --- one chunk -------------------------------------------------------------------

    async def _extract_chunk(self, chunk: Chunk, label: str) -> _ChunkOutcome:
        """Call the gateway for one chunk, halving and recursing if it will not fit."""
        if self._over_budget():
            return _ChunkOutcome(stats=[_skipped(chunk, label, "spend_cap")], events=[])

        spec = CallSpec(
            purpose="extract",
            instructions=EXTRACT_PROMPT,
            input=self._render_input(chunk),
            schema=ExtractionResult,
            max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
            reasoning_effort=EXTRACT_EFFORT,
            household_id=self.household_id,
            prompt_version=EXTRACT_PROMPT_VERSION,
        )
        try:
            result = await self.gateway.structured(spec)
        except ChunkTooLargeError:
            return await self._split_and_retry(chunk, label)
        except ContentFilteredError as error:
            # The model refused. Retrying the same text refuses again, so these messages are
            # marked extracted and the run moves on rather than looping on them forever.
            log.warning(
                "chunk %s filtered, %d messages marked extracted", label, len(chunk.primary)
            )
            return _ChunkOutcome(
                stats=[_failed(chunk, label, "filtered", error)], events=[], consumed=True
            )
        except Exception as error:
            # Deliberately broad: one bad chunk must not lose the other 300. The messages stay
            # unextracted, so the next run retries exactly them and nothing else.
            log.warning("chunk %s failed: %s", label, type(error).__name__)
            return _ChunkOutcome(stats=[_failed(chunk, label, "failed", error)], events=[])

        stat = ChunkStat(
            label=label,
            primary_count=len(chunk.primary),
            context_count=len(chunk.context),
            status="ok",
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            reasoning_tokens=result.reasoning_tokens,
            cost_usd=result.cost_usd,
            attempts=result.attempts,
            latency_ms=result.latency_ms,
        )
        events, drops, cited, invalid = self._ground(chunk, result.parsed.events)
        stat.events = len(events)
        stat.dropped = sum(drops.values())
        return _ChunkOutcome(
            stats=[stat],
            events=events,
            drops=drops,
            handles_cited=cited,
            invalid_handles=invalid,
            consumed=True,
        )

    async def _split_and_retry(self, chunk: Chunk, label: str) -> _ChunkOutcome:
        """Halve a chunk the gateway could not fit into a doubled output budget."""
        if len(chunk.primary) <= MIN_CHUNK_MESSAGES:
            log.warning("chunk %s still too large at %d messages", label, len(chunk.primary))
            return _ChunkOutcome(
                stats=[_failed(chunk, label, "failed", ChunkTooLargeError())], events=[]
            )
        half = len(chunk.primary) // 2
        first = build_chunk(chunk.primary[:half], chunk.context)
        # The second half gets the tail of the first as context, exactly as a chunk boundary
        # would have: splitting must not orphan a pronoun from its antecedent either.
        second = build_chunk(
            chunk.primary[half:], chunk.primary[max(0, half - OVERLAP_MESSAGES) : half]
        )
        parts = await asyncio.gather(
            self._extract_chunk(first, f"{label}.1"),
            self._extract_chunk(second, f"{label}.2"),
        )
        return _ChunkOutcome.combine(parts)

    def _ground(
        self, chunk: Chunk, raw: list[ExtractedEvent]
    ) -> tuple[list[_GroundedEvent], dict[str, int], int, int]:
        """Keep only what the PRIMARY messages of this chunk support.

        `handle_to_text` covers primary messages only, so an event whose sole evidence is a
        `context_only` line has no valid handle and disappears — the mechanism and the prompt
        rule are the same thing, which is why context messages cannot become sources.

        The handle strings come from the chunker, never re-derived here: the labels the model
        was shown and the labels we resolve must be formed by the same code or every citation
        silently misses.
        """
        primary = chunk.primary_by_handle()
        context = chunk.context_handles()

        cited = invalid = 0
        for event in raw:
            cited += len(event.source_message_handles)
            invalid += sum(1 for handle in event.source_message_handles if handle not in primary)

        kept = validate_against_transcript(raw, {h: (m.text or "") for h, m in primary.items()})
        kept_ids = {id(event) for event in kept}

        drops: dict[str, int] = {}
        for event in raw:
            if id(event) in kept_ids:
                continue
            handles = set(event.source_message_handles)
            if handles & primary.keys():
                reason: DropReason = "invented_quotes"
            elif handles and handles <= context:
                reason = "context_only_source"
            else:
                reason = "no_valid_handle"
            drops[reason] = drops.get(reason, 0) + 1

        grounded: list[_GroundedEvent] = []
        for event in kept:
            moment = occurred_at_utc(event.occurred_at, event.occurred_at_precision, self.tz)
            if moment is not None and not self._in_window(moment):
                drops["out_of_range_date"] = drops.get("out_of_range_date", 0) + 1
                continue
            sources = [primary[h] for h in event.source_message_handles]
            grounded.append(
                _GroundedEvent(
                    event=event,
                    sources=sources,
                    excerpts=_attribute_quotes(event.quotes, sources),
                    occurred_at_utc=moment,
                )
            )
        return grounded, drops, cited, invalid

    # --- merging ---------------------------------------------------------------------

    async def _absorb(self, chunk: Chunk, outcome: _ChunkOutcome) -> None:
        """Fold one chunk's results into the run, merging onto existing dedup keys."""
        self.result.chunks.extend(outcome.stats)
        self.result.handles_cited += outcome.handles_cited
        self.result.invalid_handles += outcome.invalid_handles
        for reason, count in outcome.drops.items():
            self.result.dropped_events[reason] = self.result.dropped_events.get(reason, 0) + count
        for stat in outcome.stats:
            self.result.total_cost_usd += stat.cost_usd
            self.result.input_tokens += stat.input_tokens
            self.result.cached_input_tokens += stat.cached_input_tokens
            self.result.output_tokens += stat.output_tokens
            self.result.reasoning_tokens += stat.reasoning_tokens

        target = (
            self.result.extracted_message_ids
            if outcome.consumed
            else self.result.unextracted_message_ids
        )
        target.extend(message.id for message in chunk.primary)

        label = outcome.stats[0].label if outcome.stats else "?"
        for grounded in outcome.events:
            await self._store(grounded, label)

    async def _store(self, grounded: _GroundedEvent, label: str) -> None:
        key = compute_dedup_key(grounded.event, self.tz)
        existing = self.by_key.get(key)
        if existing is None:
            self.by_key[key] = _record(key, grounded, label)
            return

        merged = await merge_events(
            existing.event,
            grounded.event,
            gateway=self.gateway,
            household_id=self.household_id,
        )
        if merged.used_llm:
            self.result.merge_calls += 1
            self.result.total_cost_usd += merged.cost_usd or Decimal("0")
        if merged.separate:
            # The model says these are two different events. Give the incoming one a sibling
            # key rather than losing it — a rescheduling that left the original standing is
            # exactly the case the feed must show twice.
            sibling = _sibling_key(key, self.by_key)
            self.by_key[sibling] = _record(sibling, grounded, label)
            return

        existing.event = merged.event
        existing.occurred_at_utc = occurred_at_utc(
            merged.event.occurred_at, merged.event.occurred_at_precision, self.tz
        )
        existing.mention_count += 1
        existing.merged_by_llm |= merged.used_llm
        existing.chunk_labels.append(label)
        for message in grounded.sources:
            if message.id not in existing.source_message_ids:
                existing.source_message_ids.append(message.id)
        seen = {(e.message_id, e.quote) for e in existing.source_excerpts}
        existing.source_excerpts.extend(
            excerpt
            for excerpt in grounded.excerpts
            if (excerpt.message_id, excerpt.quote) not in seen
        )

    # --- prompt input ----------------------------------------------------------------

    def _render_input(self, chunk: Chunk) -> str:
        """This run's digest of already-found events, plus the caller's own open events.

        The digest is why cross-chunk duplicates collapse at the model rather than only at the
        dedup key: told that a cardiology appointment is already on file, the model emits the
        outcome against it instead of inventing a second appointment with a different phrasing.
        """
        digest = [
            OpenEvent(
                kind=record.event.kind,
                occurred_at=(record.event.occurred_at or "undated")[:16],
                title=record.event.title,
                detail=_digest_detail(record.event),
            )
            for record in self.by_key.values()
        ]
        return render_call_input(
            chunk,
            care_brief=self.care_brief,
            tz=self.tz,
            open_events=[*self.open_events, *digest],
        )

    # --- budget ----------------------------------------------------------------------

    def _over_budget(self) -> bool:
        return self.max_spend_usd is not None and self.result.total_cost_usd >= self.max_spend_usd

    def _abandon(self, chunks: Sequence[Chunk], reason: str) -> None:
        """Stop, and leave the rest unextracted so the next run resumes from here."""
        self.result.aborted_reason = reason
        for offset, chunk in enumerate(chunks, 1):
            self.result.chunks.append(_skipped(chunk, f"skipped-{offset}", reason))
            self.result.unextracted_message_ids.extend(m.id for m in chunk.primary)
        log.warning("extraction aborted: %s, %d chunks left unextracted", reason, len(chunks))

    def _in_window(self, moment: datetime) -> bool:
        first, last = self._window
        if first is None or last is None:
            return True
        return first - DATE_SANITY_WINDOW <= moment <= last + DATE_SANITY_WINDOW


def render_call_input(
    chunk: Chunk,
    *,
    care_brief: str,
    tz: ZoneInfo,
    open_events: Sequence[OpenEvent] = (),
) -> str:
    """The one user message a chunk becomes: brief, open events, transcript.

    Public because the dry-run cost estimate must price the EXACT text the real run would
    send. An estimate built from a second, similar-looking renderer is an estimate of a
    prompt nobody uses.

    No UUIDs anywhere in it: models transpose long hex runs, so messages are cited by
    chunk-local handles and open events by `[E#]`.
    """
    parts = [f"<care_brief>\n{care_brief.strip()}\n</care_brief>"]
    block = _open_events_block(open_events)
    if block:
        parts.append(f"<open_events>\n{block}\n</open_events>")
    last = chunk.primary[-1].sent_at.astimezone(tz)
    header = f'<transcript timezone="{tz.key}" chunk_end="{last:%Y-%m-%dT%H:%M}">'
    parts.append(f"{header}\n{render_transcript(chunk, tz)}\n</transcript>")
    return "\n".join(parts)


def _open_events_block(open_events: Sequence[OpenEvent]) -> str:
    entries = list(open_events)
    # Dated events first, newest first; an undated one is the least useful line here.
    entries.sort(key=lambda e: (e.occurred_at != "undated", e.occurred_at), reverse=True)
    return "\n".join(
        f"[E{i}] {e.kind} {e.occurred_at} {e.title}" + (f" — {e.detail}" if e.detail else "")
        for i, e in enumerate(entries[:OPEN_EVENTS_LIMIT], 1)
    )


@dataclass(slots=True)
class _GroundedEvent:
    event: ExtractedEvent
    sources: list[ChunkMessage]
    excerpts: list[SourceExcerpt]
    occurred_at_utc: datetime | None


@dataclass(slots=True)
class _ChunkOutcome:
    stats: list[ChunkStat]
    events: list[_GroundedEvent]
    drops: dict[str, int] = field(default_factory=dict)
    handles_cited: int = 0
    invalid_handles: int = 0
    # True when these messages must not be offered to extraction again — successfully
    # processed, or refused by the content filter in a way retrying cannot fix.
    consumed: bool = False

    @classmethod
    def combine(cls, parts: Sequence[_ChunkOutcome]) -> _ChunkOutcome:
        merged = cls(stats=[], events=[])
        for part in parts:
            merged.stats.extend(part.stats)
            merged.events.extend(part.events)
            merged.handles_cited += part.handles_cited
            merged.invalid_handles += part.invalid_handles
            for reason, count in part.drops.items():
                merged.drops[reason] = merged.drops.get(reason, 0) + count
        # A half that failed leaves its own messages unextracted; but the parent chunk's ids
        # are stamped as one unit, so a partial success would lose the half that worked.
        # Halves are stamped together and only when every half succeeded.
        merged.consumed = all(part.consumed for part in parts)
        return merged


def _record(key: str, grounded: _GroundedEvent, label: str) -> ExtractedEventRecord:
    return ExtractedEventRecord(
        dedup_key=key,
        event=grounded.event,
        source_message_ids=[message.id for message in grounded.sources],
        source_excerpts=list(grounded.excerpts),
        occurred_at_utc=grounded.occurred_at_utc,
        chunk_labels=[label],
    )


def _sibling_key(key: str, taken: dict[str, ExtractedEventRecord]) -> str:
    candidate, index = f"{key}#2", 2
    while candidate in taken:
        index += 1
        candidate = f"{key}#{index}"
    return candidate


def _attribute_quotes(quotes: list[str], sources: Sequence[ChunkMessage]) -> list[SourceExcerpt]:
    """Pin each quote to the message it came from, so the UI can link it.

    The model returns quotes and handles as two flat lists with no correspondence between
    them, so attribution is by substring: the first cited message that actually contains the
    quote owns it. `validate_against_transcript` has already thrown away quotes no cited
    message contains, so the fallback only fires on whitespace-folding edge cases.
    """
    excerpts: list[SourceExcerpt] = []
    for quote in quotes:
        folded = " ".join(quote.split()).casefold()
        owner = next(
            (m for m in sources if folded in " ".join((m.text or "").split()).casefold()),
            sources[0] if sources else None,
        )
        if owner is None:
            continue
        excerpts.append(
            SourceExcerpt(
                message_id=owner.id,
                sent_at=owner.sent_at,
                sender=owner.sender_display_name,
                quote=quote,
            )
        )
    return excerpts


def _digest_detail(event: ExtractedEvent) -> str | None:
    return event.outcome or event.medication_action or event.symptom_name or event.note_category


def _skipped(chunk: Chunk, label: str, reason: str) -> ChunkStat:
    return ChunkStat(
        label=label,
        primary_count=len(chunk.primary),
        context_count=len(chunk.context),
        status="skipped",
        error=reason,
    )


def _failed(chunk: Chunk, label: str, status: ChunkStatus, error: Exception) -> ChunkStat:
    return ChunkStat(
        label=label,
        primary_count=len(chunk.primary),
        context_count=len(chunk.context),
        status=status,
        error=type(error).__name__,
    )


def _date_window(messages: Sequence[ChunkMessage]) -> tuple[datetime | None, datetime | None]:
    if not messages:
        return None, None
    times = [message.sent_at for message in messages]
    return min(times), max(times)
