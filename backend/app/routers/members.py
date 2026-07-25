"""The people in the chat, and the merge that fixes seeing one of them twice.

A family that imports their history AND pairs GOWA gets every person twice: a `.txt` export
gives a display name and no JID at all, GOWA gives JIDs. That is expected, it is visible, and
merging fixes attribution retroactively — without re-running the LLM over a single message.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentHousehold, SessionDep
from app.errors import NotFoundError
from app.models import Event, Member, Message
from app.schemas import MemberMerge, MemberOut

router = APIRouter(prefix="/api", tags=["members"])


@router.get("/members", response_model=list[MemberOut])
async def list_members(ctx: CurrentHousehold, session: SessionDep) -> list[MemberOut]:
    """Every member with how much they have said and when. Busiest first."""
    message_count = func.count(Message.id)
    stmt = (
        # LEFT join and a grouped aggregate rather than a correlated subquery per column: one
        # pass over a member's messages instead of three.
        select(
            Member,
            message_count.label("message_count"),
            func.min(Message.sent_at).label("first_seen_at"),
            func.max(Message.sent_at).label("last_seen_at"),
        )
        .outerjoin(Message, Message.member_id == Member.id)
        .where(Member.household_id == ctx.id)
        .group_by(Member.id)
        .order_by(message_count.desc(), Member.display_name.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        MemberOut(
            id=member.id,
            display_name=member.display_name,
            wa_jid=member.wa_jid,
            wa_lid=member.wa_lid,
            message_count=count,
            # A member created by pairing but who has not spoken yet has no messages to take
            # a range from; the contract types both fields non-null, so fall back to the row.
            first_seen_at=first_seen or member.created_at,
            last_seen_at=last_seen or member.created_at,
        )
        for member, count, first_seen, last_seen in rows
    ]


@router.post("/members/{member_id}/merge", status_code=status.HTTP_204_NO_CONTENT)
async def merge_member(
    member_id: UUID,
    body: MemberMerge,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> Response:
    """Absorb `{member_id}` into `into_member_id`. Messages and events re-point; the loser goes."""
    if member_id == body.into_member_id:
        # 400, not 404: this one is a client bug, and it says nothing about what exists.
        raise HTTPException(status_code=400, detail="A member cannot be merged into themselves.")

    loser = await _load(session, ctx.id, member_id)
    survivor = await _load(session, ctx.id, body.into_member_id)

    await session.execute(
        update(Message)
        .where(Message.household_id == ctx.id, Message.member_id == loser.id)
        .values(member_id=survivor.id)
    )
    await session.execute(
        update(Event)
        .where(Event.household_id == ctx.id, Event.actor_member_id == loser.id)
        .values(actor_member_id=survivor.id)
    )

    wa_jid = survivor.wa_jid or loser.wa_jid
    wa_lid = survivor.wa_lid or loser.wa_lid

    # The loser goes FIRST. `uq_members_household_wa_jid` and `uq_members_household_wa_lid`
    # are unique per household, so copying an identifier onto the survivor while the loser
    # still holds it is a constraint violation — the exact case a merge exists to handle.
    await session.delete(loser)
    await session.flush()

    survivor.wa_jid = wa_jid
    survivor.wa_lid = wa_lid
    await session.flush()  # never commit: get_session owns the transaction

    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _load(session: AsyncSession, household_id: UUID, member_id: UUID) -> Member:
    """Scoped to the household, so another family's member id is indistinguishable from a typo."""
    row = (
        await session.execute(
            select(Member).where(Member.id == member_id, Member.household_id == household_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError
    return row
