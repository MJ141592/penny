"""THE only function that INSERTs into `messages`.

Every adapter — the GOWA webhook, the `.txt` importer, manual entry — comes through here, and
none of them supplies a `household_id`. They hand over a `group_external_id` and the seam
resolves it. That is the tenant boundary for inbound data: a webhook body can name any chat it
likes and still cannot address a household that has not linked that chat.

THE ORDER MATTERS:

    a. group -> household        `whatsapp_links`. A miss raises `UnknownGroupError`.
                                 It NEVER auto-provisions a household.
    b. senders -> members        BEFORE hashing, so the hash can key on a resolved member.
    c. content_hash              sha256 over household, MINUTE-bucketed UTC timestamp,
                                 sender key, normalised text, and an occurrence counter.
    d. INSERT ... ON CONFLICT DO NOTHING against BOTH unique indexes.
    e. return `IngestResult`     and NEVER commit — `get_session` owns the transaction.

WHY THE MINUTE BUCKET. The same real message reaches us in two shapes. A webhook timestamp is
provider epoch-seconds; an export timestamp is local wall-clock with no seconds at all, rebuilt
from a timezone the user picked in the import wizard. A second of skew between two
representations of one message is normal; a minute is not. Bucketing to the minute is what lets
a family upload a six-month backfill *and* keep a live stream running without the overlap
appearing twice.

WHY AN OCCURRENCE COUNTER. Bucketing to the minute would otherwise erase a genuine repeat —
"ok" typed twice while the kettle boils is two messages, not one. The counter is the ordinal of
a message within its own (minute, sender, text) group, so identical repeats hash differently
while a re-upload of the same export reproduces the same ordinals and therefore the same
hashes. It is computed HERE, by the seam, not by the adapter: an adapter that saw only half a
conversation would number them differently every time.

WHY BOTH UNIQUE INDEXES. `(household_id, provider_message_id)` makes a replayed GOWA webhook
free — GOWA retries five times with backoff on any non-200. `(household_id, content_hash)` makes
re-uploading a longer export free, and is deliberately NOT scoped to provider so an export line
and the live webhook for the same message collapse into one row.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ingest.contract import IngestResult, UnknownGroupError
from app.ingest.identity import resolve_senders, sender_identity
from app.models import Message, WhatsappLink

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ingest.contract import InboundMessage
    from app.ingest.identity import IdentityResolution, SenderIdentity

log = logging.getLogger(__name__)

# Field separator inside the hashed string. Unit Separator, so it cannot occur in a WhatsApp
# message body and no amount of "|" in someone's text can shift the boundaries between fields.
_SEP = "\x1f"

# asyncpg caps a statement at 32767 bound parameters; a message row binds 15. Chunking also
# keeps a 50k-line export from building one enormous statement.
_INSERT_CHUNK = 500


def normalise_text(text: str | None) -> str:
    """NFKC, casefolded, whitespace-collapsed. The comparison form, never the stored form."""
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def minute_bucket(sent_at: datetime) -> str:
    """UTC, truncated to the minute. See the module docstring for why the second is dropped."""
    return sent_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def sender_key(identity: SenderIdentity, member_id: UUID | None) -> str:
    """What the hash uses to mean "the same person".

    A resolved member wins, always: that is the whole point of reconciling identity before
    hashing. It is what makes the export's "Mum" (a display name and nothing else) and the
    webhook's "Mum" (a JID and a LID) hash to the same value instead of duplicating six months
    of history the first time the live stream repeats a line the backfill already had.
    """
    if member_id is not None:
        return f"member:{member_id}"
    if identity.wa_jid:
        return f"jid:{identity.wa_jid}"
    if identity.wa_lid:
        return f"lid:{identity.wa_lid}"
    if identity.display_name:
        return f"name:{normalise_text(identity.display_name)}"
    return "system:"


def content_hash(
    household_id: UUID,
    sent_at: datetime,
    key: str,
    text: str | None,
    occurrence: int,
) -> bytes:
    """sha256(household | minute | sender | normalised text | occurrence) -> 32 raw bytes."""
    parts = (
        str(household_id),
        minute_bucket(sent_at),
        key,
        normalise_text(text),
        str(occurrence),
    )
    return sha256(_SEP.join(parts).encode("utf-8")).digest()


def _is_empty(message: InboundMessage) -> bool:
    """Nothing to store: no text, no media, no provider payload to re-extract from later."""
    return (
        message.message_type == "text" and not normalise_text(message.text) and not message.payload
    )


async def _resolve_household(session: AsyncSession, group_external_id: str) -> UUID:
    """Rule (a). A miss is a rejection, never a new household."""
    household_id = (
        await session.execute(
            sa.select(WhatsappLink.household_id).where(
                WhatsappLink.group_external_id == group_external_id
            )
        )
    ).scalar_one_or_none()
    if household_id is None:
        # The id is logged, never the payload: an unrecognised group is exactly the case where
        # the body is least trustworthy and most likely to belong to somebody else entirely.
        log.warning("ingest_unknown_group", extra={"group_external_id": group_external_id})
        raise UnknownGroupError(group_external_id)
    return household_id


def _build_rows(
    household_id: UUID,
    messages: Sequence[InboundMessage],
    resolution: IdentityResolution,
) -> tuple[list[dict[str, Any]], int]:
    """Rule (c). Returns the insertable rows plus how many messages were skipped as empty."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    # (minute, sender key, normalised text) -> how many we have already seen in THIS batch.
    seen: dict[tuple[str, str, str], int] = {}

    for message in messages:
        if _is_empty(message):
            skipped += 1
            continue
        identity = sender_identity(message)
        member_id = resolution.member_id_for(identity)
        key = sender_key(identity, member_id)
        sent_at = message.sent_at.astimezone(UTC)
        group = (minute_bucket(sent_at), key, normalise_text(message.text))
        occurrence = seen.get(group, 0)
        seen[group] = occurrence + 1
        rows.append(
            {
                "id": uuid4(),
                "household_id": household_id,
                "provider": message.provider,
                "provider_message_id": message.provider_message_id,
                "content_hash": content_hash(household_id, sent_at, key, message.text, occurrence),
                "sender_wa_jid": identity.wa_jid,
                "sender_wa_lid": identity.wa_lid,
                "sender_display_name": message.sender_display_name,
                "member_id": member_id,
                "sent_at": sent_at,
                "source_ordinal": message.source_ordinal,
                "message_type": message.message_type,
                "text": message.text,
                "payload": message.payload or {},
            }
        )
    return rows, skipped


def _dedupe_in_batch(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse rows that collide with each other before Postgres has to.

    A single statement carrying two rows with the same content_hash is not a conflict Postgres
    can resolve against the index (neither is committed yet), so an in-batch duplicate would
    slip through ON CONFLICT and land twice. Filter first, then let the index handle history.
    """
    unique: list[dict[str, Any]] = []
    hashes: set[bytes] = set()
    provider_ids: set[str] = set()
    duplicates = 0
    for row in rows:
        pid = row["provider_message_id"]
        if row["content_hash"] in hashes or (pid is not None and pid in provider_ids):
            duplicates += 1
            continue
        hashes.add(row["content_hash"])
        if pid is not None:
            provider_ids.add(pid)
        unique.append(row)
    return unique, duplicates


async def ingest_messages(
    session: AsyncSession,
    group_external_id: str,
    messages: Sequence[InboundMessage],
) -> IngestResult:
    """The one door into `messages`. See the module docstring for the order and the rules.

    Never commits: `get_session` owns the transaction, so a webhook that fails after this call
    leaves no half-written batch behind.
    """
    household_id = await _resolve_household(session, group_external_id)

    if not messages:
        return IngestResult(
            received=0,
            inserted=0,
            duplicates=0,
            skipped=0,
            new_member_ids=[],
            first_sent_at=None,
            last_sent_at=None,
            household_id=household_id,
        )

    # (b) BEFORE hashing — the hash keys on the resolved member.
    resolution = await resolve_senders(
        session, household_id, (sender_identity(m) for m in messages)
    )

    rows, skipped = _build_rows(household_id, messages, resolution)
    rows, duplicates = _dedupe_in_batch(rows)

    inserted = 0
    for start in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[start : start + _INSERT_CHUNK]
        # (d) No conflict target: DO NOTHING covers BOTH partial unique indexes at once, so a
        # replayed provider_message_id and a re-uploaded export line are equally free.
        statement = pg_insert(Message).values(chunk).on_conflict_do_nothing().returning(Message.id)
        result = await session.execute(statement)
        inserted += len(result.scalars().all())
    duplicates += len(rows) - inserted

    considered = [m.sent_at.astimezone(UTC) for m in messages if not _is_empty(m)]
    result_ = IngestResult(
        received=len(messages),
        inserted=inserted,
        duplicates=duplicates,
        skipped=skipped,
        new_member_ids=resolution.new_member_ids,
        first_sent_at=min(considered, default=None),
        last_sent_at=max(considered, default=None),
        household_id=household_id,
    )
    # Counts and ids only. The rule is absolute: message text never reaches a log line.
    log.info(
        "ingest_complete",
        extra={
            "household_id": str(household_id),
            "group_external_id": group_external_id,
            "received": result_.received,
            "inserted": result_.inserted,
            "duplicates": result_.duplicates,
            "skipped": result_.skipped,
            "new_members": len(result_.new_member_ids),
        },
    )
    return result_
