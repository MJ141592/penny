"""Sender -> member reconciliation. Who said this, and is it somebody we already know?

THE LADDER, in this order and no other:

    1. exact `wa_jid`      ('4477...@s.whatsapp.net')
    2. exact `wa_lid`      ('2515...@lid')
    3. folded `display_name` (NFKC + casefold + collapsed whitespace)
    4. create a provisional member

Both identifiers, always. WhatsApp is migrating to `@lid` and GOWA emits both; a group event
may carry a `@lid` sender for a participant whose phone number the client cannot resolve. Key
on one alone and the same person arrives as two members — one holding the history, one holding
the new messages, and the feed silently attributes half a care log to a stranger.

FILL IN, NEVER MERGE. Two rules pull in opposite directions and only one of them is safe to get
wrong:

* When a message carries an identifier a matched member is *missing*, we write it in. That is
  how "Mum" from a `.txt` export (display name, no JID at all) becomes the same member as
  "Mum" from a live webhook (JID + LID). Filling a NULL cannot mis-attribute anything.
* When a match would require reconciling **two different** `wa_jid`s — a display-name hit on a
  member who already has a different JID, a `wa_lid` hit whose JID contradicts — we refuse and
  create a separate provisional member instead. A wrong merge writes one person's medical
  messages under another person's name and there is no undo. A wrong split is two rows in a
  member list that a human can look at.

Provisional members are real rows, immediately: `messages.member_id` is a foreign key, and the
content hash keys on the resolved member, so identity has to exist before a message can be
written. "Provisional" means nobody has confirmed the name, not that the row is a draft.

This module NEVER commits. It flushes, because the caller needs the ids.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy as sa

from app.models import Member

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ingest.contract import InboundMessage

log = logging.getLogger(__name__)

# A JID may arrive with a device suffix ('4477...:12@s.whatsapp.net') when the sender has
# linked devices. The device is a transport detail, not a person: strip it or one human
# fragments into a member per phone, tablet and desktop.
_DEVICE_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """The normalised identifiers a single message carries about its sender.

    Frozen and hashable on purpose: it is the cache key for a batch, so a 200-message export
    from four people costs four resolutions.
    """

    wa_jid: str | None
    wa_lid: str | None
    display_name: str | None

    @property
    def is_empty(self) -> bool:
        """No identifiers at all — a structural system line ("Messages are encrypted")."""
        return not (self.wa_jid or self.wa_lid or self.display_name)


@dataclass(slots=True)
class IdentityResolution:
    """The outcome for a whole batch: every distinct sender mapped to exactly one member."""

    member_ids: dict[SenderIdentity, UUID] = field(default_factory=dict)
    new_member_ids: list[UUID] = field(default_factory=list)

    def member_id_for(self, identity: SenderIdentity) -> UUID | None:
        return self.member_ids.get(identity)


def normalise_jid(raw: str | None) -> str | None:
    """'+4477...:12@S.WhatsApp.net' -> '4477...@s.whatsapp.net'. Same for '@lid'."""
    if raw is None:
        return None
    value = unicodedata.normalize("NFKC", raw).strip().lower().lstrip("+")
    if not value:
        return None
    user, at, server = value.partition("@")
    if not at:
        return value
    user = user.split(_DEVICE_SEPARATOR, 1)[0]
    return f"{user}@{server}" if user else None


def fold_name(raw: str | None) -> str | None:
    """NFKC + casefold + whitespace-collapse. 'Sarah  J.' and 'sarah j.' are one person."""
    if raw is None:
        return None
    folded = " ".join(unicodedata.normalize("NFKC", raw).casefold().split())
    return folded or None


def sender_identity(message: InboundMessage) -> SenderIdentity:
    """Read the sender off an `InboundMessage`, normalised. The adapter's spelling is not law."""
    return SenderIdentity(
        wa_jid=normalise_jid(message.sender_wa_jid),
        wa_lid=normalise_jid(message.sender_wa_lid),
        display_name=(message.sender_display_name or "").strip() or None,
    )


class _MemberIndex:
    """The household's members, indexed the three ways the ladder looks them up.

    Held in memory for the batch and mutated as members are created or filled in, so the
    second message from a brand-new sender resolves to the member the first one created
    instead of racing it.
    """

    def __init__(self, members: Sequence[Member]) -> None:
        self.by_jid: dict[str, Member] = {}
        self.by_lid: dict[str, Member] = {}
        self.by_name: dict[str, list[Member]] = {}
        for member in members:
            self.add(member)

    def add(self, member: Member) -> None:
        if member.wa_jid:
            self.by_jid[member.wa_jid] = member
        if member.wa_lid:
            self.by_lid[member.wa_lid] = member
        folded = fold_name(member.display_name)
        if folded:
            self.by_name.setdefault(folded, []).append(member)


def _compatible(member: Member, identity: SenderIdentity) -> bool:
    """False when accepting this match would silently merge two distinct identities."""
    if member.wa_jid and identity.wa_jid and member.wa_jid != identity.wa_jid:
        return False
    return not (member.wa_lid and identity.wa_lid and member.wa_lid != identity.wa_lid)


def _fill_in(member: Member, identity: SenderIdentity, index: _MemberIndex) -> None:
    """Write identifiers the member is missing. Only ever NULL -> value, never value -> value."""
    if identity.wa_jid and not member.wa_jid and identity.wa_jid not in index.by_jid:
        member.wa_jid = identity.wa_jid
        index.by_jid[identity.wa_jid] = member
        log.info("member_identifier_filled", extra={"member_id": str(member.id), "field": "wa_jid"})
    if identity.wa_lid and not member.wa_lid and identity.wa_lid not in index.by_lid:
        member.wa_lid = identity.wa_lid
        index.by_lid[identity.wa_lid] = member
        log.info("member_identifier_filled", extra={"member_id": str(member.id), "field": "wa_lid"})
    # A blank display name is a placeholder, not a choice a human made. Anything else is left
    # alone: the family may have renamed "+44 7700 900123" to "Mum" and an inbound push
    # notification name must not undo that.
    if identity.display_name and not (member.display_name or "").strip():
        member.display_name = identity.display_name


def _match(identity: SenderIdentity, index: _MemberIndex) -> Member | None:
    """The ladder itself. Returns None when rung 4 (create) is the only honest answer."""
    if identity.wa_jid:
        member = index.by_jid.get(identity.wa_jid)
        if member is not None:
            # The JID is the strongest key we have; a contradicting LID does not unseat it.
            return member
    if identity.wa_lid:
        member = index.by_lid.get(identity.wa_lid)
        if member is not None and _compatible(member, identity):
            return member
        if member is not None:
            log.warning(
                "identity_conflict_lid_jid_mismatch",
                extra={"member_id": str(member.id), "resolution": "new_member"},
            )
            return None
    folded = fold_name(identity.display_name)
    if folded:
        candidates = [m for m in index.by_name.get(folded, []) if _compatible(m, identity)]
        if candidates:
            # Deterministic when a name is shared: the member who has been here longest wins,
            # so re-running an import cannot flip attribution between two same-named rows.
            return min(candidates, key=lambda m: (m.created_at is None, m.created_at, str(m.id)))
        if index.by_name.get(folded):
            log.warning("identity_conflict_name_jid_mismatch", extra={"resolution": "new_member"})
    return None


def _provisional(household_id: UUID, identity: SenderIdentity, index: _MemberIndex) -> Member:
    """Rung 4. Claim only the identifiers no existing member already holds."""
    display_name = identity.display_name or _fallback_name(identity)
    member = Member(
        id=uuid4(),
        household_id=household_id,
        display_name=display_name,
        wa_jid=identity.wa_jid if identity.wa_jid not in index.by_jid else None,
        wa_lid=identity.wa_lid if identity.wa_lid not in index.by_lid else None,
    )
    return member


def _fallback_name(identity: SenderIdentity) -> str:
    """A member row needs a name. Show the phone/lid user part, not the raw JID."""
    source = identity.wa_jid or identity.wa_lid or ""
    user = source.partition("@")[0]
    return f"+{user}" if user.isdigit() else (user or "Unknown")


async def resolve_senders(
    session: AsyncSession,
    household_id: UUID,
    identities: Iterable[SenderIdentity],
) -> IdentityResolution:
    """Map every distinct sender in a batch to exactly one member of `household_id`.

    Creates the members it has to, fills in identifiers it can, and flushes — the seam needs
    real ids for `messages.member_id` and for the content hash. It does NOT commit.
    """
    distinct = {i for i in identities if not i.is_empty}
    resolution = IdentityResolution()
    if not distinct:
        return resolution

    existing = (
        await session.execute(sa.select(Member).where(Member.household_id == household_id))
    ).scalars()
    index = _MemberIndex(list(existing))

    created: list[Member] = []
    # Sorted for determinism: two runs over the same export must create members in the same
    # order, or a re-import produces differently-ordered `new_member_ids` for no reason.
    for identity in sorted(
        distinct, key=lambda i: (i.wa_jid or "", i.wa_lid or "", i.display_name or "")
    ):
        member = _match(identity, index)
        if member is None:
            member = _provisional(household_id, identity, index)
            session.add(member)
            index.add(member)
            created.append(member)
            resolution.new_member_ids.append(member.id)
        else:
            _fill_in(member, identity, index)
        resolution.member_ids[identity] = member.id

    # Flush, not commit: the seam is about to insert messages that FK to these rows.
    await session.flush()
    if created:
        log.info(
            "members_provisioned",
            extra={"household_id": str(household_id), "count": len(created)},
        )
    return resolution
