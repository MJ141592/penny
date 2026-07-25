"""The two read routes the app is built around: the feed, and what is coming up.

"Upcoming" is a QUERY (`occurred_at > now()`), not an entity. No table, no column, no flag —
which is why an event that slips from future to past needs no migration, no cron and no
write: it simply stops matching one predicate and starts matching the other.

Both routes filter on `ctx.id`. That filter is the entire tenancy mechanism; there is no RLS
behind it to catch a query that forgets.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import CurrentHousehold, SessionDep
from app.models import Event, Member
from app.schemas import (
    FEED_DEFAULT_LIMIT,
    FEED_MAX_LIMIT,
    UPCOMING_LIMIT,
    FeedPage,
    UpcomingPage,
    to_event,
)

router = APIRouter(prefix="/api", tags=["feed"])


def _rows_to_events(rows: list[tuple[Event, str | None]]) -> list:
    return [to_event(event, display_name) for event, display_name in rows]


@router.get("/feed", response_model=FeedPage)
async def get_feed(
    ctx: CurrentHousehold,
    session: SessionDep,
    limit: int = Query(FEED_DEFAULT_LIMIT, ge=1, le=FEED_MAX_LIMIT),
    before: datetime | None = Query(None),
) -> FeedPage:
    """Newest first, deleted rows excluded, one page at a time.

    `before` is exclusive on `occurred_at` only. Known and accepted: events sharing the exact
    boundary timestamp can be skipped across a page edge. With a few hundred events per
    household and a day-grouped feed, real keyset pagination protects nothing here.
    """
    stmt = (
        # LEFT join: `actor_member_id` is nullable and an unattributable event must still
        # appear in the feed. An inner join here silently hides events, which is the kind of
        # bug nobody reports because the row never shows up to be doubted.
        select(Event, Member.display_name)
        .outerjoin(Member, Event.actor_member_id == Member.id)
        .where(Event.household_id == ctx.id, Event.deleted_at.is_(None))
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(limit)
    )
    if before is not None:
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        stmt = stmt.where(Event.occurred_at < before)

    rows = list((await session.execute(stmt)).all())
    events = _rows_to_events(rows)
    # Null when a partial page came back: that is the client's stop signal, and returning a
    # cursor unconditionally makes it fetch the same empty tail forever.
    next_before = events[-1].occurred_at if len(events) == limit else None
    return FeedPage(events=events, next_before=next_before)


@router.get("/upcoming", response_model=UpcomingPage)
async def get_upcoming(ctx: CurrentHousehold, session: SessionDep) -> UpcomingPage:
    """Everything still to come, soonest first. Ascending, unlike the feed, and capped at 50."""
    stmt = (
        select(Event, Member.display_name)
        .outerjoin(Member, Event.actor_member_id == Member.id)
        .where(
            Event.household_id == ctx.id,
            Event.deleted_at.is_(None),
            Event.occurred_at > datetime.now(UTC),
        )
        .order_by(Event.occurred_at.asc(), Event.id.asc())
        .limit(UPCOMING_LIMIT)
    )
    rows = list((await session.execute(stmt)).all())
    return UpcomingPage(events=_rows_to_events(rows))
