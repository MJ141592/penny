"""The frozen ingestion contract — the ONE door inbound messages come through.

THE INVARIANT: the caller never supplies `household_id`.

Every adapter (the GOWA webhook, the `.txt` export importer, manual entry) hands the seam a
`group_external_id` and nothing else. Group -> household resolution happens *inside*
`ingest_messages`, because that resolution IS the tenant boundary for inbound data: a webhook
body can name any chat it likes and still cannot address a household it is not linked to. Put
the lookup in the adapter and every adapter has to get tenancy right; put it in the seam and
none of them can get it wrong.

An unrecognised group raises `UnknownGroupError` and is logged. It NEVER auto-provisions a
household — auto-provisioning from an unauthenticated payload is the tenant-leak vector.

`group_external_id` is either a GOWA `chat_id` (`"1203...@g.us"`) or, for a `.txt` upload,
the sentinel `export:<household_id>` produced by `export_group_external_id()`.

DELIBERATE v1 OMISSIONS — documented, not silently unhandled. Message edits, deletions and
reactions are dropped at the adapter and never become `InboundMessage`s. Consequences we accept:
an edited message keeps its original text in the feed, a deleted message stays in the history,
and reactions carry no signal into extraction. `messages` is append-only by design; changing
that is a schema decision, not an adapter decision.

Nothing in this module imports the database, the app, or a provider SDK. It is pure stdlib so
both sides of the seam can be built and tested before the other exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

Provider = Literal["gowa", "whatsapp_export", "manual"]

# GOWA message events carry NO `is_group` field. A group is detected purely by suffix:
# chat_id.endswith(GROUP_JID_SUFFIX). Direct chats are ignored, not ingested.
GROUP_JID_SUFFIX = "@g.us"


def export_group_external_id(household_id: UUID) -> str:
    """Sentinel group id for `.txt` uploads, which have no WhatsApp chat_id of their own."""
    return f"export:{household_id}"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message as an adapter saw it, before it belongs to any household."""

    provider: Provider
    provider_message_id: str | None  # GOWA payload.id; None for a .txt export
    sender_wa_jid: str | None  # '4477...@s.whatsapp.net'; None for a .txt export
    sender_wa_lid: str | None  # '2515...@lid' — GOWA emits both, key on BOTH
    sender_display_name: str | None  # GOWA from_name, or the name in the export line
    sent_at: datetime  # MUST be tz-aware; normalised to UTC by the seam
    text: str | None  # None for media with no caption
    message_type: str = "text"
    payload: dict[str, Any] = field(default_factory=dict)  # verbatim; re-extraction needs it
    # Line index in a .txt export. The stable tiebreak when many messages share a minute —
    # export timestamps have no seconds, so ordering is otherwise arbitrary.
    source_ordinal: int | None = None

    def __post_init__(self) -> None:
        # Every downstream date computation assumes tz-aware UTC: feed day dividers, dedup
        # minute buckets, and "yesterday" resolution during extraction. A naive datetime is
        # silently wrong in all three and throws in none of them, so reject it here.
        if self.sent_at.tzinfo is None or self.sent_at.tzinfo.utcoffset(self.sent_at) is None:
            raise ValueError(
                f"sent_at must be timezone-aware (UTC), got naive {self.sent_at!r}. "
                "Attach the timezone the adapter parsed with; do not assume the server's."
            )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What the seam did. Frozen because callers report it, they don't accumulate into it."""

    received: int
    inserted: int
    duplicates: int  # already present via provider_message_id or content_hash
    skipped: int  # unsupported message_type, empty text with no payload, etc.
    new_member_ids: list[UUID]  # members auto-created from unseen senders
    first_sent_at: datetime | None
    last_sent_at: datetime | None
    household_id: UUID  # resolved INSIDE the seam; the caller never passed it in


class UnknownGroupError(Exception):
    """`group_external_id` maps to no household. Reject and log — never auto-provision."""

    def __init__(self, group_external_id: str) -> None:
        super().__init__(f"No household is linked to group {group_external_id!r}")
        self.group_external_id = group_external_id


@runtime_checkable
class MessageSink(Protocol):
    """The seam, as seen by an adapter — so Track F and Track A can be typed and faked now.

    The real implementation is `app.ingest.seam.ingest_messages`; tests substitute a recorder.
    """

    async def ingest_messages(
        self,
        session: AsyncSession,
        group_external_id: str,
        messages: Sequence[InboundMessage],
    ) -> IngestResult: ...
