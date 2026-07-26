"""Editing and deleting one event. Both operations are about making a human's decision stick.

An edit sets `edited_at` and records which fields the human touched, and that is what makes it
permanent: re-extraction skips a row a person has edited rather than overwriting their wording
with the model's next attempt.

A delete is SOFT. The row and — critically — its `dedup_key` survive, so the next extraction
run matches the tombstone and stops instead of resurrecting the event. Hard-deleting here
would mean every re-run silently brings back everything the family deleted, which is the most
infuriating bug this app could have and the hardest to diagnose from a bug report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Response, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentHousehold, SessionDep
from app.errors import NotFoundError, ValidationError
from app.models import Event, Member
from app.schemas import DETAILS_MODEL, AnyEvent, EventCreate, EventPatch, to_event

router = APIRouter(prefix="/api", tags=["events"])


@router.post("/events", response_model=AnyEvent, status_code=status.HTTP_201_CREATED)
async def create_event(
    create: EventCreate,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> AnyEvent:
    """Create a manual entry that extraction can never merge into or overwrite."""
    title = create.title.strip()
    if not title:
        raise ValidationError("A title is required.")
    model = DETAILS_MODEL[create.kind]
    try:
        details = model.model_validate(create.details).model_dump(mode="json")
    except PydanticValidationError as exc:
        raise ValidationError(_detail_message(exc)) from exc

    event_id = uuid4()
    occurred_at = create.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    event = Event(
        id=event_id,
        household_id=ctx.id,
        kind=create.kind,
        occurred_at=occurred_at,
        occurred_at_precision=create.occurred_at_precision,
        title=title,
        body=create.body.strip() if create.body else None,
        details=details,
        source_message_ids=[],
        source_excerpts=[],
        occurrences=[],
        user_edited_fields=[],
        dedup_key=f"human:{event_id}",
        pinned=True,
    )
    session.add(event)
    await session.flush()
    return to_event(event)


async def _load(
    session: AsyncSession,
    household_id: UUID,
    event_id: UUID,
    *,
    include_deleted: bool = False,
) -> tuple[Event, str | None]:
    """Fetch one event with its actor's name, scoped to the household.

    The `household_id` predicate is not an optimisation. Without it this route hands any
    signed-in family any other family's event, and nothing downstream would notice.
    """
    stmt = (
        select(Event, Member.display_name)
        .outerjoin(Member, Event.actor_member_id == Member.id)
        .where(Event.id == event_id, Event.household_id == household_id)
    )
    if not include_deleted:
        stmt = stmt.where(Event.deleted_at.is_(None))
    row = (await session.execute(stmt)).first()
    if row is None:
        # 404, never 403 — a 403 confirms the row exists somewhere, and this response has to
        # be byte-identical to the one for a random UUID.
        raise NotFoundError
    return row[0], row[1]


@router.patch("/events/{event_id}", response_model=AnyEvent)
async def patch_event(
    event_id: UUID,
    patch: EventPatch,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> AnyEvent:
    """Full trust: anyone signed in edits anything. `details` is merged, not replaced."""
    event, actor_display_name = await _load(session, ctx.id, event_id)

    # `model_fields_set` and not a truthiness check: `body: null` and `body` omitted are
    # different requests, and only one of them means "clear the body".
    provided = patch.model_fields_set
    edited: list[str] = []

    if "title" in provided and patch.title is not None:
        event.title = patch.title
        edited.append("title")
    if "body" in provided:
        event.body = patch.body
        edited.append("body")
    if "occurred_at" in provided and patch.occurred_at is not None:
        moment = patch.occurred_at
        event.occurred_at = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
        edited.append("occurred_at")
    if "occurred_at_precision" in provided and patch.occurred_at_precision is not None:
        event.occurred_at_precision = patch.occurred_at_precision
        edited.append("occurred_at_precision")
    if "details" in provided and patch.details is not None:
        merged = {**(event.details or {}), **patch.details}
        model = DETAILS_MODEL.get(event.kind, DETAILS_MODEL["note"])
        try:
            validated = model.model_validate(merged)
        except PydanticValidationError as exc:
            raise ValidationError(_detail_message(exc)) from exc
        # Reassigned, never mutated in place: SQLAlchemy does not track mutation inside a
        # JSONB dict, so an in-place update produces a 200 that wrote nothing.
        event.details = validated.model_dump(mode="json")
        edited.extend(f"details.{key}" for key in patch.details)

    if not edited:
        return to_event(event, actor_display_name)

    event.edited_at = datetime.now(UTC)
    # Same reassignment rule as `details`: a Postgres ARRAY column appended to in place is
    # invisible to the unit of work.
    event.user_edited_fields = sorted(set(event.user_edited_fields or []) | set(edited))
    # No session.commit() — get_session owns the transaction and commits when this returns.
    await session.flush()
    return to_event(event, actor_display_name)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: UUID,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> Response:
    """Soft delete. Gone from the feed immediately; the tombstone lives forever."""
    # Deleted rows are included on purpose, so deleting twice is a quiet 204 rather than an
    # error toast on a double click. It leaks nothing: a row from another household still 404s.
    event, _ = await _load(session, ctx.id, event_id, include_deleted=True)
    if event.deleted_at is None:
        event.deleted_at = datetime.now(UTC)
        await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _detail_message(exc: PydanticValidationError) -> str:
    """One renderable sentence, with no rejected values in it."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "Invalid value")
    return f"details.{location}: {message}." if location else f"{message}."
