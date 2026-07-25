"""The `.txt` import wizard: preview, commit, poll.

NOBODY DISCOVERS THE PRICE OF AN IMPORT BY PAYING IT. `POST /api/imports/preview` parses the
file, stores nothing, and answers with the parse report, the sniffed settings and a COST
ESTIMATE priced from the exact prompt text a real run would send — "about $19, roughly 25
minutes" — so the confirm button is an informed decision. `IMPORT_MAX_SPEND_USD` then refuses
anything over the ceiling as a 409 before a single token is bought.

`dayfirst` AND `timezone` ARE REQUIRED ON THE COMMIT, WITH NO SERVER-SIDE DEFAULT. `dd/mm` vs
`mm/dd` is genuinely undecidable in an export spanning under twelve days, and an export carries
no UTC offset at all. `sniff()` proposes; this route only ever does what the user confirmed,
and stores both choices on the `imports` row so a later bug report is reproducible.

THE UPLOAD IS STREAMED TO A TEMPORARY FILE. A 25 MB export read whole, decoded and split costs
three copies of itself in RAM inside a process that is also serving requests, and the parser
already reads line by line for exactly that reason.

`imports` HAS NO `extracted_count` COLUMN and will not grow one. The contract returns the
number, so it is COUNTed over `messages.extracted_at` — a derived number that cannot drift from
the cursor, which is the only thing that actually decides what still needs extracting.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import CurrentHousehold, SessionDep
from app.errors import BudgetExceededError, ConflictError, NotFoundError, ValidationError
from app.extraction.chunker import build_chunks
from app.extraction.runner import (
    MAX_CONCURRENT_CHUNKS,
    as_chunk_messages,
    render_call_input,
)
from app.extraction.service import build_care_brief, run_extraction_for_household
from app.ingest.contract import export_group_external_id
from app.ingest.seam import ingest_messages
from app.ingest.whatsapp_txt import (
    MAX_BYTES,
    ExportOptions,
    ExportTooLargeError,
    ParseReport,
    parse_export,
    sniff,
)
from app.llm.pricing import cost_usd
from app.llm.prompts import EXTRACT_PROMPT
from app.llm.schemas import ExtractionResult, to_strict_json_schema
from app.models import Household, Import, Message, WhatsappLink

if TYPE_CHECKING:
    from app.ingest.contract import InboundMessage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/imports", tags=["imports"])

# 1 MiB at a time: big enough that a 25 MB upload is 25 awaits, small enough that a hundred
# concurrent uploads cannot each pin 25 MB of heap.
UPLOAD_CHUNK = 1024 * 1024
# `sniff()` only needs enough lines to see a timestamp with a first component over 12.
SNIFF_LINES = 200
# How long the background job waits for a concurrent run to release the household's lock.
# Three tries at 5s/10s/15s covers a webhook-triggered run of a handful of new messages,
# which is the only thing realistically holding it when an import lands.
LOCK_RETRIES = 3
LOCK_RETRY_SECONDS = 5
# How long a spawned job waits for the request that scheduled it to commit. Milliseconds in
# practice; the ceiling only matters when the request failed and will never commit at all.
COMMIT_WAIT_TRIES = 60
COMMIT_WAIT_SECONDS = 0.25

# Strong references to detached extraction tasks. See `_spawn_extraction`.
_RUNNING: set[asyncio.Task[None]] = set()

# --- the cost estimate ---------------------------------------------------------------
# Deliberately a copy of the arithmetic in `scripts/extract_export.py` rather than an import
# of it: `app` must not depend on `scripts` (the container does not ship it, and the
# dependency would run backwards). Both price the SAME rendered prompt text via
# `render_call_input`, which is the part that actually has to agree.

# ~4 characters per token for English chat. Good to about +/-10%, the right accuracy for a
# number whose only job is to stop someone spending $19 by accident.
CHARS_PER_TOKEN = 4
# ~250 reasoning tokens plus ~3 events at ~170 tokens each.
OUTPUT_TOKENS_PER_CHUNK = 760
# 334 chunks in ~17 minutes at concurrency 4, from the plan's 40k-message backfill row.
SECONDS_PER_CHUNK = 12


class Estimate(BaseModel):
    message_count: int
    estimated_cost_usd: str
    estimated_minutes: int
    budget_usd: str
    over_budget: bool


class PreviewMessage(BaseModel):
    sent_at: str
    sender: str | None
    text: str | None


class Sniffed(BaseModel):
    dayfirst: bool
    dayfirst_evidence: str
    timezone: str
    timezone_source: str


class Report(BaseModel):
    total_lines: int
    messages: int
    continuations: int
    system_lines: int
    media_placeholders: int
    unparsed_lines: int
    unparsed_samples: list[str]
    detected_format: str
    senders: dict[str, int]
    first_sent_at: str | None
    last_sent_at: str | None
    preview_head: list[PreviewMessage]
    preview_tail: list[PreviewMessage]


class PreviewOut(BaseModel):
    filename: str
    file_sha256: str
    report: Report
    sniffed: Sniffed
    estimate: Estimate


class ImportAccepted(BaseModel):
    import_id: UUID
    message_count: int
    estimated_cost_usd: str


class ImportStatus(BaseModel):
    status: str
    message_count: int
    inserted_count: int
    extracted_count: int
    error: str | None


@router.post("/preview", response_model=PreviewOut)
async def preview_import(
    session: SessionDep,
    ctx: CurrentHousehold,
    file: Annotated[UploadFile, File()],
) -> PreviewOut:
    """Parse in memory, store NOTHING, and quote a price."""
    path, sha256, _ = await _spool(file)
    try:
        hints = sniff(_head(path, SNIFF_LINES))
        tz = ZoneInfo(ctx.household.timezone)
        messages, report = _parse(path, dayfirst=hints.dayfirst, tz=tz)
        if report.messages == 0:
            raise ValidationError("No WhatsApp messages found in that file.")
        estimate = await _estimate(session, ctx.household, messages, tz)
    finally:
        _remove(path)

    return PreviewOut(
        filename=file.filename or "export.txt",
        file_sha256=sha256,
        report=_report_out(report),
        sniffed=Sniffed(
            dayfirst=hints.dayfirst,
            dayfirst_evidence=hints.dayfirst_evidence,
            timezone=ctx.household.timezone,
            timezone_source="household_default",
        ),
        estimate=estimate,
    )


@router.post("", status_code=202, response_model=ImportAccepted)
async def create_import(
    session: SessionDep,
    ctx: CurrentHousehold,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    dayfirst: Annotated[bool, Form()],
    timezone: Annotated[str, Form()],
) -> ImportAccepted:
    """Ingest the file with the settings the user CONFIRMED, then extract in the background."""
    if timezone not in available_timezones():
        raise ValidationError(f"{timezone!r} is not a known timezone.")
    tz = ZoneInfo(timezone)

    path, sha256, _ = await _spool(file)
    try:
        if await _already_imported(session, ctx.id, sha256):
            raise ConflictError("This export has already been imported.")
        messages, report = _parse(path, dayfirst=dayfirst, tz=tz)
        if report.messages == 0:
            raise ValidationError("No WhatsApp messages found in that file.")
        estimate = await _estimate(session, ctx.household, messages, tz)
    finally:
        _remove(path)

    settings = get_settings()
    if estimate.over_budget:
        # A 409, not a 402: docs/api-contract.md has no 402 anywhere in it.
        raise BudgetExceededError(
            f"That import would cost about ${estimate.estimated_cost_usd}, "
            f"over the ${settings.import_max_spend_usd} limit."
        )

    record = Import(
        household_id=ctx.id,
        file_sha256=sha256,
        filename=file.filename or "export.txt",
        # What we handed the seam, INCLUDING system lines, so `inserted_count` is always a
        # subset of it and the progress denominator cannot exceed 100%.
        message_count=len(messages),
        dayfirst=dayfirst,
        timezone=timezone,
        status="importing",
    )
    session.add(record)
    await session.flush()

    group_external_id = await _link_for_export(session, ctx.id)
    result = await ingest_messages(session, group_external_id, messages)
    record.inserted_count = result.inserted
    record.status = "extracting"
    await session.flush()

    log.info(
        "import accepted household=%s import=%s messages=%d inserted=%d duplicates=%d",
        ctx.id,
        record.id,
        result.received,
        result.inserted,
        result.duplicates,
    )
    background.add_task(_spawn_extraction, ctx.id, record.id, settings.import_max_spend_usd)
    return ImportAccepted(
        import_id=record.id,
        message_count=len(messages),
        estimated_cost_usd=estimate.estimated_cost_usd,
    )


@router.get("/{import_id}", response_model=ImportStatus)
async def get_import(session: SessionDep, ctx: CurrentHousehold, import_id: UUID) -> ImportStatus:
    """404, never 403, for an import that is missing OR belongs to another household."""
    record = (
        await session.execute(
            sa.select(Import).where(Import.id == import_id, Import.household_id == ctx.id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError()
    return ImportStatus(
        status=record.status,
        message_count=record.message_count,
        inserted_count=record.inserted_count,
        extracted_count=await _extracted_count(session, ctx.id, record.created_at),
        error=record.error,
    )


# --- the background job ---------------------------------------------------------------


async def _spawn_extraction(household_id: UUID, import_id: UUID, max_spend_usd: Decimal) -> None:
    """DETACH the work from the request, and this is not gratuitous.

    On FastAPI 0.140 / Starlette 1.3 a `yield` dependency is torn down AFTER the response's
    background tasks have run, so `get_session`'s commit happens *last*. A background task
    that does its work inline therefore opens a second connection and sees NOTHING: no
    messages, no `imports` row, `extracted_at IS NULL` on 395 rows it cannot read. It logs a
    cheerful `ran=True events=+0` and the import sits at "extracting" for ever — and its own
    "complete" write silently updates zero rows, because the row it targets is still invisible.
    Not a hypothetical: this is exactly what the first end-to-end run of this route did.

    Awaiting the commit from inside the background task would DEADLOCK — the commit is
    sequenced after us in the same coroutine. So the work is spawned as an independent task
    that waits for the transaction to become visible, and this callback returns immediately so
    the request can get on with committing it.
    """
    task = asyncio.create_task(_extract_in_background(household_id, import_id, max_spend_usd))
    # asyncio keeps only a WEAK reference to a running task; without this the garbage
    # collector may cancel an import mid-run, at random, under load.
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def _extract_in_background(
    household_id: UUID, import_id: UUID, max_spend_usd: Decimal
) -> None:
    """Run extraction, then record how it went. TWO transactions, deliberately.

    The extraction commits on its own. If the status update were in the same transaction, a
    failure while writing "complete" would roll back every event the run just paid for — and
    the second session is also the only way to record a FAILURE, since the first one is by
    then rolled back.
    """
    maker = get_sessionmaker()
    if not await _await_commit(maker, household_id, import_id):
        # The request rolled back after all, so there is no import and nothing to extract.
        log.warning("import never committed household=%s import=%s", household_id, import_id)
        return

    status, error = "complete", None
    try:
        summary = None
        for attempt in range(LOCK_RETRIES):
            async with maker() as session:
                summary = await run_extraction_for_household(
                    session, household_id, max_spend_usd=max_spend_usd
                )
                await session.commit()
            if summary.ran:
                break
            # Another run holds the household's advisory lock — a webhook, or the cron. It
            # started BEFORE our messages landed, so it may not cover them; wait for it to
            # finish rather than declaring an import complete that has not been extracted.
            await asyncio.sleep(LOCK_RETRY_SECONDS * (attempt + 1))
        assert summary is not None
        if not summary.ran or summary.aborted_reason or summary.messages_unextracted:
            # A spend abort leaves the remainder with `extracted_at IS NULL`, which is
            # exactly what the hourly cron looks for. The import stays "extracting" and
            # self-heals rather than lying that it finished.
            #
            # `messages_unextracted` covers the other way that happens, and it is NOT
            # hypothetical: a chunk whose call errors or whose response will not parse is
            # caught per-chunk, so a run in which every single chunk failed still returns
            # `ran=True, aborted_reason=None`. Without this clause that import reports
            # "complete" with an `extracted_count` of just the system lines, and the family
            # is shown a finished import and an empty feed with nothing to retry.
            status = "extracting"
        log.info(
            "import extraction done household=%s import=%s ran=%s events=+%d/~%d cost=%s "
            "stamped=%d unextracted=%d aborted=%s",
            household_id,
            import_id,
            summary.ran,
            summary.events_inserted,
            summary.events_updated,
            summary.cost_usd,
            summary.messages_stamped,
            summary.messages_unextracted,
            summary.aborted_reason,
        )
    except Exception as exc:  # the job has no caller to raise to
        status, error = "failed", _client_sentence(exc)
        log.exception("import extraction failed household=%s import=%s", household_id, import_id)

    async with maker() as session:
        await session.execute(
            sa.update(Import)
            .where(Import.id == import_id, Import.household_id == household_id)
            .values(status=status, error=error)
        )
        await session.commit()


async def _await_commit(maker: Any, household_id: UUID, import_id: UUID) -> bool:
    """Block until the `imports` row is visible from a fresh connection.

    The import row and its messages are written in ONE transaction, so seeing the row is
    proof that the messages are readable too — which is the precondition this whole job has,
    and the one thing it must not assume.
    """
    for _ in range(COMMIT_WAIT_TRIES):
        async with maker() as session:
            found = (
                await session.execute(
                    sa.select(Import.id).where(
                        Import.id == import_id, Import.household_id == household_id
                    )
                )
            ).first()
        if found is not None:
            return True
        await asyncio.sleep(COMMIT_WAIT_SECONDS)
    return False


def _client_sentence(exc: Exception) -> str:
    """`imports.error` is rendered verbatim to the family, so it can never be a traceback."""
    from app.errors import PennyError

    if isinstance(exc, PennyError):
        return exc.detail
    return "Extraction did not finish. It will be retried automatically."


# --- upload plumbing ------------------------------------------------------------------


async def _spool(file: UploadFile) -> tuple[Path, str, int]:
    """Stream to a temp file, hashing as it goes. Never `await file.read()` with no argument."""
    digest = hashlib.sha256()
    total = 0
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")  # noqa: SIM115
    path = Path(handle.name)
    try:
        while chunk := await file.read(UPLOAD_CHUNK):
            total += len(chunk)
            if total > MAX_BYTES:
                raise HTTPException(413, "That file is larger than 25 MB.")
            digest.update(chunk)
            handle.write(chunk)
    except BaseException:
        handle.close()
        _remove(path)
        raise
    finally:
        handle.close()
    return path, digest.hexdigest(), total


def _remove(path: Path) -> None:
    """Sync unlink from async code, on purpose: this is a local temp file, not IO worth a
    thread hop, and leaking a 25 MB spool file is worse than one microsecond of blocking."""
    path.unlink(missing_ok=True)


def _head(path: Path, lines: int) -> str:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return "".join(line for _, line in zip(range(lines), handle, strict=False))


def _parse(path: Path, *, dayfirst: bool, tz: ZoneInfo) -> tuple[list[InboundMessage], ParseReport]:
    """`errors="replace"`: one mojibake glyph beats losing the family's whole history."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return parse_export(handle, ExportOptions(tz=tz, dayfirst=dayfirst))
    except ExportTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc


# --- estimating -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Priced:
    chunks: int
    messages: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    minutes: int


async def _estimate(
    session: SessionDep, household: Household, messages: list[InboundMessage], tz: ZoneInfo
) -> Estimate:
    settings = get_settings()
    priced = _price(
        as_chunk_messages(messages),
        care_brief=await build_care_brief(session, household),
        tz=tz,
        model=settings.llm_model_extract,
    )
    return Estimate(
        message_count=priced.messages,
        estimated_cost_usd=f"{priced.cost_usd:.2f}",
        estimated_minutes=priced.minutes,
        budget_usd=f"{settings.import_max_spend_usd:.2f}",
        over_budget=priced.cost_usd > settings.import_max_spend_usd,
    )


def _price(chunk_messages: list[Any], *, care_brief: str, tz: ZoneInfo, model: str) -> _Priced:
    """Price the EXACT prompts a real run would send, without sending any of them."""
    chunks = build_chunks(chunk_messages)
    fixed = _tokens(EXTRACT_PROMPT) + _tokens(json.dumps(to_strict_json_schema(ExtractionResult)))
    input_tokens = sum(
        fixed + _tokens(render_call_input(chunk, care_brief=care_brief, tz=tz)) for chunk in chunks
    )
    output_tokens = OUTPUT_TOKENS_PER_CHUNK * len(chunks)
    seconds = (len(chunks) / MAX_CONCURRENT_CHUNKS) * SECONDS_PER_CHUNK
    return _Priced(
        chunks=len(chunks),
        messages=sum(len(chunk.primary) for chunk in chunks),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, input_tokens, output_tokens),
        minutes=max(1, round(seconds / 60)),
    )


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


# --- small queries --------------------------------------------------------------------


async def _already_imported(session: SessionDep, household_id: UUID, sha256: str) -> bool:
    """Dedup on the FILE hash. A re-uploaded LONGER export is a different file and is welcome:
    per-message `content_hash` stops the overlap duplicating."""
    return (
        await session.execute(
            sa.select(Import.id).where(
                Import.household_id == household_id, Import.file_sha256 == sha256
            )
        )
    ).first() is not None


async def _extracted_count(session: SessionDep, household_id: UUID, since: datetime) -> int:
    """Progress, counted over the cursor rather than a column that could drift from it.

    Bounded by `ingested_at >= import.created_at` so a household's earlier history does not
    make a fresh import look finished the moment it starts.
    """
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(
                Message.household_id == household_id,
                Message.ingested_at >= since,
                Message.extracted_at.isnot(None),
            )
        )
    ).scalar_one()


async def _link_for_export(session: SessionDep, household_id: UUID) -> str:
    """The `group_external_id` this upload ingests under.

    `whatsapp_links.household_id` is the PRIMARY KEY — one linked group per household — so an
    export cannot mint its own sentinel row for a household that has already paired a real
    GOWA group. It reuses the paired chat id instead, which is also the correct answer: an
    export of that same chat and the live feed from it are one conversation, and ingesting
    them under one group is what makes `content_hash` collapse the overlap.
    """
    existing = (
        await session.execute(
            sa.select(WhatsappLink.group_external_id).where(
                WhatsappLink.household_id == household_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    sentinel = export_group_external_id(household_id)
    session.add(
        WhatsappLink(
            household_id=household_id,
            group_external_id=sentinel,
            status="linked",
            linked_at=sa.func.now(),
        )
    )
    await session.flush()
    return sentinel


def _report_out(report: ParseReport) -> Report:
    return Report(
        total_lines=report.total_lines,
        messages=report.messages,
        continuations=report.continuations,
        system_lines=report.system_lines,
        media_placeholders=report.media_placeholders,
        unparsed_lines=report.unparsed_lines,
        unparsed_samples=report.unparsed_samples,
        detected_format=report.detected_format,
        senders=report.senders,
        first_sent_at=_iso(report.first_sent_at),
        last_sent_at=_iso(report.last_sent_at),
        preview_head=[_preview(m) for m in report.preview_head],
        preview_tail=[_preview(m) for m in report.preview_tail],
    )


def _preview(message: InboundMessage) -> PreviewMessage:
    return PreviewMessage(
        sent_at=_iso(message.sent_at) or "",
        sender=message.sender_display_name,
        text=message.text,
    )


def _iso(value: datetime | None) -> str | None:
    """`...Z`, not `+00:00`: docs/api-contract.md spells every timestamp with a Z."""
    if value is None:
        return None
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
