"""`ExtractionRunResult` -> rows. The one place extraction output becomes durable.

Everything upstream of here is pure: the runner has no session, `merge` has no session, and
that inversion is what let the riskiest part of the product be built and evaluated before a
schema existed. This module pays that debt back. It is also the highest-risk file in the
milestone, because until it existed *nothing had ever executed the path from an
`ExtractedEventRecord` into a row of `events`*.

FIVE RULES, EACH OF WHICH FAILS SILENTLY IF BROKEN

1. **A human edit is permanent.** The upsert's `WHERE events.edited_at IS NULL` is the whole
   mechanism. `merge.as_stored_view()` hardcodes `edited_at=None` — correct for the M1 runner,
   which has no rows — so anything asking "has a human touched this?" must read the REAL row.
   `stored_view()` below is that adapter; the SQL guard is the enforcement.

2. **Never `RETURNING` from a conditional upsert.** `ON CONFLICT DO UPDATE ... WHERE` returns
   NO ROW when the WHERE fails, so a `RETURNING id` silently yields fewer ids than rows
   written and the caller cannot tell which ones it lost. Ids are always SELECTed separately.

3. **There is no confidence guard.** A `NULL >= NULL` comparison is NULL, not true, so a
   `WHERE EXCLUDED.confidence >= events.confidence` would drop every re-extraction on the
   floor and look exactly like the model getting worse.

4. **`occurred_at` is NOT NULL.** Postgres `DESC` sorts NULLS FIRST, so a nullable column
   would pin every undated event to the top of the feed forever. An event the model could not
   date falls back to the earliest message it was extracted from — which is always a date the
   family can recognise.

5. **`extracted_at` is stamped for EXACTLY the messages the runner reports extracted.**
   Stamped-but-unextracted is data loss with no error; extracted-but-unstamped is a second
   bill for work already paid for. Neither throws.

BRIDGING THE M1 -> M4 GAPS. Three columns existed with nothing producing them, and
`ExtractedEventRecord` carries `mention_count`/`chunk_labels`, which are not their shape:

  * `occurrences` — one append-only entry per PERSIST, carrying the run's own
    `mention_count` and `chunk_labels`. "Mentioned 3x" is the sum of `mention_count` across
    entries, which stays true across re-extractions in a way `len(occurrences)` would not: a
    single run can already have merged three mentions into one record before it gets here.
  * `user_edited_fields` — a FINER guard than `edited_at`. Rule 1 stops a whole edited row
    from being rewritten; this stops one hand-corrected column being rewritten on a row that
    is otherwise still the model's. Enforced per column in the `DO UPDATE SET`.
  * `pinned` — set by Split, meaning "a human deliberately separated this". A pinned row is
    never merged onto: the incoming record is re-keyed to a `<key>#N` sibling instead, exactly
    as the runner does for a DIFFERENT-EVENTS verdict, so the split survives the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.extraction.merge import as_stored_view
from app.models import Event, Member, Message

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.extraction.runner import ExtractedEventRecord, ExtractionRunResult, SourceExcerpt

log = logging.getLogger(__name__)

# asyncpg's protocol caps a statement at 32,767 bound parameters. `extracted_at` stamping is
# one parameter per id, so a 21,000-message backfill has to go in slices or the whole import
# fails at the very last step, after all the money has been spent.
_ID_BATCH = 5_000

# Columns a human can hand-correct, and which a later extraction must therefore leave alone
# when they appear in `events.user_edited_fields`. Named exactly as the API exposes them, so
# a PATCH handler can store the field name it received without translating.
PROTECTABLE_FIELDS = (
    "kind",
    "title",
    "body",
    "occurred_at",
    "occurred_at_precision",
    "details",
    "actor_member_id",
)


@dataclass(slots=True)
class PersistResult:
    """What actually reached the database. Every number here is a thing that can go wrong."""

    inserted: int = 0
    updated: int = 0
    # An existing row was edited, pinned or soft-deleted, so extraction left it alone.
    protected: int = 0
    # The event cited only messages this household does not have. Never silently zero-ed:
    # a caller that fed the runner `local_message_id()` values instead of real `messages.id`
    # would otherwise see "0 events" and no reason why.
    orphaned: int = 0
    stamped: int = 0
    event_ids: list[UUID] = field(default_factory=list)


def stored_view(row: Any) -> dict[str, Any]:
    """`merge.as_stored_view()` for a REAL row — the version that knows about human edits.

    `as_stored_view()` builds the same shape from an `ExtractedEvent` and can only hardcode
    `edited_at=None`, because an `ExtractedEvent` has never been in a database. Anything
    calling `dedup.decide_merge()` against something already stored must use this one, or the
    "a human edit is permanent" rule quietly stops applying at the only moment it matters.
    """
    return {
        "kind": row.kind,
        "edited_at": row.edited_at,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "occurred_at_precision": row.occurred_at_precision,
        "title": row.title,
        "body": row.body,
        "details": dict(row.details or {}),
    }


async def persist_extraction(
    session: AsyncSession,
    household_id: UUID,
    result: ExtractionRunResult,
    *,
    extraction_run_id: UUID | None = None,
    now: datetime | None = None,
) -> PersistResult:
    """Write one run's events and stamp its messages. Never commits — see `app.db`."""
    now = now or datetime.now(UTC)
    outcome = PersistResult()

    rows = await _prepare(session, household_id, result.events, outcome)
    if rows:
        payloads = [
            _payload(row, household_id=household_id, extraction_run_id=extraction_run_id, now=now)
            for row in rows
        ]
        await session.execute(_upsert(payloads, now))
        outcome.event_ids = await _select_ids(session, household_id, [r.key for r in rows])

    outcome.stamped = await _stamp(session, household_id, result.extracted_message_ids, now)
    log.info(
        "extraction persisted household=%s inserted=%d updated=%d protected=%d orphaned=%d "
        "stamped=%d",
        household_id,
        outcome.inserted,
        outcome.updated,
        outcome.protected,
        outcome.orphaned,
        outcome.stamped,
    )
    return outcome


# --- preparing one row ----------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    """One record resolved against what is already in the database."""

    key: str
    record: ExtractedEventRecord
    occurred_at: datetime
    source_message_ids: list[UUID]
    source_excerpts: list[dict[str, Any]]
    occurrences: list[dict[str, Any]]
    actor_member_id: UUID | None


async def _prepare(
    session: AsyncSession,
    household_id: UUID,
    records: Sequence[ExtractedEventRecord],
    outcome: PersistResult,
) -> list[_Row]:
    if not records:
        return []
    sent_at = await _source_sent_at(session, household_id, records)
    actors = await _members_by_name(session, household_id)
    existing = await _existing(session, household_id, [r.dedup_key for r in records])

    rows: dict[str, _Row] = {}
    for record in records:
        sources = [mid for mid in record.source_message_ids if mid in sent_at]
        if not sources:
            outcome.orphaned += 1
            continue

        key = record.dedup_key
        prior = existing.get(key)
        if prior is not None and prior.pinned:
            # A human used Split on this key. Never merge onto it; the incoming record becomes
            # a sibling, which is also where the NEXT run will land, so this is stable.
            key, prior = await _sibling(session, household_id, key)
        if prior is not None and (prior.deleted_at is not None or prior.edited_at is not None):
            # A tombstone is what stops the next run resurrecting everything the family
            # deleted; an edit is permanent. Both are left exactly as they are.
            outcome.protected += 1
            continue
        if key in rows:
            # Two records collapsing onto one key in the same statement is
            # "ON CONFLICT DO UPDATE cannot affect row a second time" — a hard error that
            # loses the whole batch. Fold them here instead.
            _fold(rows[key], record, sources, sent_at)
            continue

        excerpts = _excerpts(record.source_excerpts)
        rows[key] = _Row(
            key=key,
            record=record,
            occurred_at=record.occurred_at_utc or min(sent_at[mid] for mid in sources),
            source_message_ids=_union(prior.source_message_ids if prior else [], sources),
            source_excerpts=_merge_excerpts(prior.source_excerpts if prior else [], excerpts),
            occurrences=list(prior.occurrences) if prior else [],
            actor_member_id=_actor(record, actors),
        )
        if prior is None:
            outcome.inserted += 1
        else:
            outcome.updated += 1
    return list(rows.values())


def _payload(
    row: _Row, *, household_id: UUID, extraction_run_id: UUID | None, now: datetime
) -> dict[str, Any]:
    view = as_stored_view(row.record.event)
    return {
        "household_id": household_id,
        "kind": row.record.event.kind,
        "occurred_at": row.occurred_at,
        "occurred_at_precision": row.record.occurred_at_precision,
        "title": _clean(row.record.event.title) or "(untitled)",
        "body": _clean(row.record.event.body),
        "details": _clean_json(view["details"]),
        "actor_member_id": row.actor_member_id,
        "source_message_ids": row.source_message_ids,
        "source_excerpts": row.source_excerpts,
        "occurrences": [
            *row.occurrences,
            {
                "at": now.isoformat(),
                "source_message_ids": [str(mid) for mid in row.source_message_ids],
                "llm_run_id": str(extraction_run_id) if extraction_run_id else None,
                # The runner already merged this many mentions into one record before we saw
                # it, so "mentioned Nx" is the SUM of these, not the number of entries.
                "mention_count": row.record.mention_count,
                "chunk_labels": list(row.record.chunk_labels),
            },
        ],
        "dedup_key": row.key,
        "extraction_run_id": extraction_run_id,
        "updated_at": now,
    }


def _upsert(payloads: list[dict[str, Any]], now: datetime) -> Any:
    stmt = pg_insert(Event).values(payloads)
    excluded = stmt.excluded
    set_: dict[str, Any] = {
        name: _unless_hand_edited(name, getattr(excluded, name)) for name in PROTECTABLE_FIELDS
    }
    set_.update(
        {
            # Evidence is additive and already unioned in Python, where duplicates can be
            # removed by (message_id, quote). Doing it as `events.x || EXCLUDED.x` in SQL is
            # simpler to write and grows the array without bound on every re-extraction.
            "source_message_ids": excluded.source_message_ids,
            "source_excerpts": excluded.source_excerpts,
            "occurrences": excluded.occurrences,
            "extraction_run_id": excluded.extraction_run_id,
            "updated_at": now,
        }
    )
    return stmt.on_conflict_do_update(
        index_elements=[Event.household_id, Event.dedup_key],
        set_=set_,
        # The last line of defence, and the reason nothing here uses RETURNING: a row that
        # fails this predicate is simply not written, and the statement reports no row for it.
        where=sa.and_(
            Event.edited_at.is_(None),
            Event.deleted_at.is_(None),
            Event.pinned.is_(False),
        ),
    )


def _unless_hand_edited(name: str, incoming: Any) -> Any:
    """Keep the stored value when a human has corrected THIS column by hand."""
    return sa.case(
        (sa.literal(name) == sa.any_(Event.user_edited_fields), getattr(Event, name)),
        else_=incoming,
    )


# --- reads ----------------------------------------------------------------------------


async def _source_sent_at(
    session: AsyncSession, household_id: UUID, records: Sequence[ExtractedEventRecord]
) -> dict[UUID, datetime]:
    """`{message_id: sent_at}` for this household only — the tenant filter AND the fallback.

    Scoping to `household_id` is not belt-and-braces: `source_message_ids` arrives from a
    model-driven pipeline, and an id that belongs to another family must not become a source
    excerpt on this family's feed.
    """
    ids = {mid for record in records for mid in record.source_message_ids}
    found: dict[UUID, datetime] = {}
    for batch in _batched(sorted(ids), _ID_BATCH):
        rows = await session.execute(
            sa.select(Message.id, Message.sent_at).where(
                Message.household_id == household_id, Message.id.in_(batch)
            )
        )
        found.update(rows.all())  # type: ignore[arg-type]
    return found


async def _members_by_name(session: AsyncSession, household_id: UUID) -> dict[str, UUID]:
    """Display name (folded) -> member id, ambiguous names excluded.

    Two members called "Sarah" cannot be told apart from a first name in a transcript, so
    attributing to either is worse than attributing to neither: a wrong actor on a care event
    is a thing the family will read and believe.
    """
    rows = await session.execute(
        sa.select(Member.display_name, Member.id).where(Member.household_id == household_id)
    )
    by_name: dict[str, UUID] = {}
    ambiguous: set[str] = set()
    for display_name, member_id in rows.all():
        key = (display_name or "").strip().casefold()
        if not key:
            continue
        if key in by_name:
            ambiguous.add(key)
        by_name[key] = member_id
    for key in ambiguous:
        by_name.pop(key, None)
    return by_name


async def _existing(
    session: AsyncSession, household_id: UUID, keys: Sequence[str]
) -> dict[str, Any]:
    if not keys:
        return {}
    found: dict[str, Any] = {}
    for batch in _batched(sorted(set(keys)), _ID_BATCH):
        rows = await session.execute(
            sa.select(
                Event.id,
                Event.dedup_key,
                Event.edited_at,
                Event.deleted_at,
                Event.pinned,
                Event.source_message_ids,
                Event.source_excerpts,
                Event.occurrences,
            ).where(Event.household_id == household_id, Event.dedup_key.in_(batch))
        )
        found.update({row.dedup_key: row for row in rows.all()})
    return found


async def _sibling(session: AsyncSession, household_id: UUID, key: str) -> tuple[str, Any]:
    """The `<key>#N` a pinned row's would-be merge lands on instead.

    Mirrors the runner's DIFFERENT-EVENTS sibling keys, so a Split and a model disagreement
    produce the same shape. The first usable sibling is REUSED rather than a new one minted,
    or every run would add another near-duplicate card to the feed.
    """
    rows = await session.execute(
        sa.select(
            Event.id,
            Event.dedup_key,
            Event.edited_at,
            Event.deleted_at,
            Event.pinned,
            Event.source_message_ids,
            Event.source_excerpts,
            Event.occurrences,
        ).where(
            Event.household_id == household_id,
            Event.dedup_key.like(f"{key}#%"),
        )
    )
    siblings = {row.dedup_key: row for row in rows.all()}
    index = 2
    while True:
        candidate = f"{key}#{index}"
        row = siblings.get(candidate)
        if row is None:
            return candidate, None
        if not row.pinned and row.deleted_at is None and row.edited_at is None:
            return candidate, row
        index += 1


async def _select_ids(session: AsyncSession, household_id: UUID, keys: Sequence[str]) -> list[UUID]:
    """The ids, read back rather than RETURNed — see rule 2 in the module docstring."""
    ids: list[UUID] = []
    for batch in _batched(sorted(set(keys)), _ID_BATCH):
        rows = await session.execute(
            sa.select(Event.id).where(
                Event.household_id == household_id, Event.dedup_key.in_(batch)
            )
        )
        ids.extend(rows.scalars())
    return ids


# --- writes ---------------------------------------------------------------------------


async def _stamp(
    session: AsyncSession, household_id: UUID, message_ids: Sequence[UUID], now: datetime
) -> int:
    """Mark exactly the messages the runner says it processed, and no others.

    `extracted_at IS NULL` in the predicate keeps this idempotent: a re-run that overlaps an
    earlier one must not move the timestamp on messages already paid for, because the column
    is also the only record of WHEN a message cost money.
    """
    stamped = 0
    for batch in _batched(list(dict.fromkeys(message_ids)), _ID_BATCH):
        result = await session.execute(
            sa.update(Message)
            .where(
                Message.household_id == household_id,
                Message.id.in_(batch),
                Message.extracted_at.is_(None),
            )
            .values(extracted_at=now)
        )
        stamped += result.rowcount or 0
    return stamped


# --- small pure helpers ---------------------------------------------------------------


def _fold(row: _Row, record: ExtractedEventRecord, sources: list[UUID], sent_at: dict) -> None:
    """Absorb a second record that resolved onto an already-claimed key."""
    row.source_message_ids = _union(row.source_message_ids, sources)
    row.source_excerpts = _merge_excerpts(row.source_excerpts, _excerpts(record.source_excerpts))
    row.record.mention_count += record.mention_count
    row.record.chunk_labels.extend(record.chunk_labels)
    if record.occurred_at_utc and record.occurred_at_utc < row.occurred_at:
        row.occurred_at = record.occurred_at_utc


def _actor(record: ExtractedEventRecord, members: dict[str, UUID]) -> UUID | None:
    for name in record.event.actors:
        member_id = members.get((name or "").strip().casefold())
        if member_id is not None:
            return member_id
    return None


def _excerpts(excerpts: Sequence[SourceExcerpt]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": str(excerpt.message_id),
            "sent_at": excerpt.sent_at.isoformat(),
            "sender": _clean(excerpt.sender),
            "quote": _clean(excerpt.quote) or "",
        }
        for excerpt in excerpts
    ]


def _merge_excerpts(
    existing: Sequence[dict[str, Any]], incoming: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union by (message_id, quote), oldest first. Never shortens: the family reads these."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for excerpt in (*existing, *incoming):
        merged.setdefault((str(excerpt.get("message_id")), str(excerpt.get("quote"))), excerpt)
    return sorted(merged.values(), key=lambda e: str(e.get("sent_at") or ""))


def _union(existing: Iterable[UUID], incoming: Iterable[UUID]) -> list[UUID]:
    return list(dict.fromkeys([*existing, *incoming]))


def _clean(value: str | None) -> str | None:
    """Make a model-authored string storable. Two characters, both fatal to the WHOLE batch.

    **NUL.** Postgres `text` cannot hold `\\x00` and rejects the statement outright, so one
    stray NUL — in the model's output, or in a mangled export line it quoted verbatim — fails
    every event in the run with `A string literal cannot contain NUL`.

    **Lone surrogates.** This is the one that will actually happen. `\\ud83d` is the high half
    of most emoji, WhatsApp chat is made of emoji, and a model that truncates mid-emoji emits
    exactly that half. `json.loads` — and therefore `model_validate_json` — passes an unpaired
    `\\udXXX` escape straight through into a Python `str` that is NOT encodable as UTF-8, and
    asyncpg then raises `DataError: 'utf-8' codec can't encode character '\\ud83d'`. It is the
    same shape of bug as the NUL one, in the same untested gap, and it costs the same thing:
    the entire extraction run is rolled back after every chunk has already been paid for.

    Dropping the half-character is right. It is not recoverable — there is no second half —
    and losing one glyph from a quote is a better outcome than losing the family's history.
    """
    if value is None:
        return None
    value = value.replace("\x00", "")
    # `isascii()` is a fast path: the check below allocates, and almost every string is clean.
    if not value.isascii():
        value = value.encode("utf-8", "ignore").decode("utf-8")
    return value


def _clean_json(value: Any) -> Any:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {key: _clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    return value


def _batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
