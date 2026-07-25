"""Every shape the HTTP API accepts or returns, and the ONE place a row becomes an `Event`.

Two model shapes exist in this repo, deliberately (see `docs/api-contract.md`):

* the LLM emits one **flat** `ExtractedEvent` — strict structured outputs reject a
  `discriminator` and reject a non-object root — and `extraction.merge._details` folds its
  kind-prefixed fields (`symptom_name`, `medication_action`, `note_category`) into the
  per-kind `details` object that gets stored in `events.details`;
* the API returns a **discriminated union** on `kind`, so `switch (event.kind)` is exhaustive
  in TypeScript and a new kind fails `tsc -b` at every render site.

`to_event()` below is the only bridge between a database row and that union. Every route that
returns an event goes through it, so a rename lands in one place instead of four.

Precision is spelled the CONTRACT's way (`exact`/`day`/…), never the model's (`datetime`/
`date`/…). The translation already exists in `extraction.runner.normalise_precision` and is
imported rather than rewritten: a second copy is a second thing to get out of step, and the
failure mode is a value the `events.occurred_at_precision` CHECK constraint rejects.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, ValidationError

from app.extraction.runner import normalise_precision

logger = logging.getLogger(__name__)


def _iso_utc(value: datetime) -> str:
    """ISO 8601 in UTC with a trailing `Z`, never a local offset.

    The client renders every timestamp in `household.timezone` with `Intl`, so a response
    carrying `+01:00` is not wrong-looking, it is wrong twice: once in the raw value and
    again after the browser converts it.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str)]

# A stored JSONB string that the contract types non-null but the extraction model may have
# left null: `ExtractedEvent.symptom_name` and `medication_name` are `str | None`, and
# `merge._details` passes them straight through. Null there must cost that one field, not the
# whole feed page — an empty string renders as nothing, a 500 renders as nothing at all.
NullableStr = Annotated[str, BeforeValidator(lambda v: "" if v is None else v)]

EventKind = Literal["symptom", "appointment", "medication", "note"]
OccurredAtPrecision = Literal["exact", "day", "week", "month", "unknown"]
Severity = Literal["mild", "moderate", "severe", "unknown"]
AppointmentKind = Literal["gp", "specialist", "hospital", "test", "therapy", "other"]
AppointmentStatus = Literal["scheduled", "attended", "cancelled", "missed"]
MedicationAction = Literal[
    "started", "stopped", "changed", "missed", "refilled", "side_effect", "other"
]
NoteCategory = Literal["logistics", "mood", "finance", "equipment", "admin", "other"]

FEED_DEFAULT_LIMIT = 200
FEED_MAX_LIMIT = 500
UPCOMING_LIMIT = 50
MIN_PASSWORD_CHARS = 12
TITLE_MAX_CHARS = 80
BODY_MAX_CHARS = 400


class SourceExcerpt(BaseModel):
    """Verbatim evidence. This is what makes the feed trustworthy enough to show a family."""

    model_config = ConfigDict(extra="ignore")

    message_id: UUID
    sent_at: UtcDatetime
    # The contract types this `string`; the extraction runner's excerpt allows None for a
    # message whose sender never resolved. Empty string renders as nothing, which is honest —
    # substituting a placeholder name would put a fabricated attribution next to a real quote.
    sender: NullableStr = ""
    quote: NullableStr = ""


class EventActor(BaseModel):
    """Who did or reported it — the WhatsApp sender, not whoever is signed in."""

    member_id: UUID
    display_name: str


# --- per-kind `details` -------------------------------------------------------------------
#
# Every field has a default. `events.details` is JSONB written by the extraction pipeline, and
# a feed that 500s because one row is missing one key is a worse outcome than a feed with one
# thin card in it. `extra="ignore"` for the same reason: an older row written by an earlier
# prompt version must still render.


class SymptomDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symptom: NullableStr = ""
    severity: Severity = "unknown"
    body_site: str | None = None
    duration_text: str | None = None


class AppointmentDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    appointment_kind: AppointmentKind = "other"
    provider_name: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    outcome: str | None = None
    follow_up_actions: list[str] = Field(default_factory=list)
    # An appointment is ONE event for its whole life: it is created `scheduled` and gains an
    # outcome and `attended` when the family reports back, on the same row via `dedup_key`.
    status: AppointmentStatus = "scheduled"


class MedicationDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    medication_name: NullableStr = ""
    dose_text: str | None = None
    action: MedicationAction = "other"
    prescriber: str | None = None


class NoteDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: NoteCategory = "other"


class EventBase(BaseModel):
    id: UUID
    # NEVER null: it falls back to the earliest source message's `sent_at`, because Postgres
    # `DESC` sorts NULLS FIRST and undated events would otherwise pin to the top of the feed.
    occurred_at: UtcDatetime
    occurred_at_precision: OccurredAtPrecision
    title: str
    body: str | None = None
    actor: EventActor | None = None
    source_excerpts: list[SourceExcerpt] = Field(default_factory=list)
    # Non-null means a human edited it and re-extraction will never overwrite it again.
    edited_at: UtcDatetime | None = None
    created_at: UtcDatetime


class SymptomEvent(EventBase):
    kind: Literal["symptom"] = "symptom"
    details: SymptomDetails


class AppointmentEvent(EventBase):
    kind: Literal["appointment"] = "appointment"
    details: AppointmentDetails


class MedicationEvent(EventBase):
    kind: Literal["medication"] = "medication"
    details: MedicationDetails


class NoteEvent(EventBase):
    kind: Literal["note"] = "note"
    details: NoteDetails


AnyEvent = Annotated[
    SymptomEvent | AppointmentEvent | MedicationEvent | NoteEvent,
    Field(discriminator="kind"),
]

_EVENT_MODEL: dict[str, type[EventBase]] = {
    "symptom": SymptomEvent,
    "appointment": AppointmentEvent,
    "medication": MedicationEvent,
    "note": NoteEvent,
}

# Used by PATCH to validate a merged `details` object before it is stored, so a typo'd key
# from the edit form is a 422 rather than a field that quietly stops rendering.
DETAILS_MODEL: dict[str, type[BaseModel]] = {
    "symptom": SymptomDetails,
    "appointment": AppointmentDetails,
    "medication": MedicationDetails,
    "note": NoteDetails,
}


def _coerce_details(model: type[BaseModel], raw: Any) -> dict[str, Any]:
    """Drop what a stored `details` object cannot legally contain, so the field falls back.

    Tolerant on READ only. `events.details` is JSONB written by whichever prompt version was
    live at the time, and a value that no longer parses — a null `symptom`, a `severity` an
    older prompt allowed — must cost that one field rather than 500 the whole feed page. The
    write path (PATCH) validates the same models strictly, because there a rejected value is a
    typo the person can still fix.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        field = model.model_fields.get(key)
        if field is None:
            continue  # a key from a newer or older shape; `extra="ignore"` would drop it anyway
        annotation = field.annotation
        if value is None and type(None) not in get_args(annotation):
            continue
        if get_origin(annotation) is Literal and value not in get_args(annotation):
            logger.warning("schemas.details_value_dropped", extra={"field": key})
            continue
        cleaned[key] = value
    return cleaned


def _excerpts(raw: Any) -> list[dict[str, Any]]:
    """Keep the excerpts that parse, drop the ones that don't, never fail the request.

    An excerpt is evidence attached to an event, not the event. One malformed entry from an
    older prompt version must cost that quote, not the whole feed page.
    """
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            SourceExcerpt.model_validate(item)
        except ValidationError:
            logger.warning("schemas.excerpt_dropped")  # no text, ever — ids and counts only
            continue
        kept.append(item)
    return kept


def to_event(row: Any, actor_display_name: str | None = None) -> Any:
    """A database row -> the per-kind union the browser sees. The only conversion, on purpose.

    `row` is an `app.models.Event`; it is typed loosely so this module does not import the ORM
    just to annotate one parameter. `actor_display_name` comes from the caller's join, because
    doing it here would be a lazy load per row on an async session.
    """
    kind = row.kind if row.kind in _EVENT_MODEL else "note"
    actor = None
    if row.actor_member_id is not None:
        actor = {
            "member_id": row.actor_member_id,
            "display_name": actor_display_name or "",
        }
    return _EVENT_MODEL[kind].model_validate(
        {
            "id": row.id,
            "kind": kind,
            "occurred_at": row.occurred_at,
            # The one spelling bridge, imported not rewritten.
            "occurred_at_precision": normalise_precision(row.occurred_at_precision),
            "title": row.title,
            "body": row.body,
            "actor": actor,
            "details": _coerce_details(DETAILS_MODEL[kind], row.details),
            "source_excerpts": _excerpts(row.source_excerpts),
            "edited_at": row.edited_at,
            "created_at": row.created_at,
        }
    )


# --- feed ---------------------------------------------------------------------------------


class FeedPage(BaseModel):
    events: list[AnyEvent]
    # The `occurred_at` of the last event returned, or null when there is no next page. The
    # client stops paginating on null; returning the cursor unconditionally makes it loop.
    next_before: UtcDatetime | None = None


class UpcomingPage(BaseModel):
    """An empty list is the common case and must render as an empty state, not a spinner."""

    events: list[AnyEvent]


class EventPatch(BaseModel):
    """All fields optional; `details` is MERGED key by key, not replaced.

    `extra="forbid"` so a typo'd field name is a 422 the client can render, rather than a 200
    that silently changed nothing — the worst possible outcome for an edit form.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_CHARS)
    body: str | None = Field(default=None, max_length=BODY_MAX_CHARS)
    occurred_at: datetime | None = None
    occurred_at_precision: OccurredAtPrecision | None = None
    details: dict[str, Any] | None = None


# --- household ----------------------------------------------------------------------------


class HouseholdOut(BaseModel):
    id: UUID
    name: str
    care_recipient_name: str
    timezone: str


def to_household(row: Any) -> HouseholdOut:
    """The `household` object, identical in `GET /api/me` and `PATCH /api/household`.

    One function so the two can never drift — and so neither can ever include
    `password_hash` by reaching for `model_validate(row, from_attributes=True)`.
    """
    return HouseholdOut(
        id=row.id,
        name=row.name,
        care_recipient_name=row.care_recipient_name,
        timezone=row.timezone,
    )


class SessionCounts(BaseModel):
    events: int
    messages: int


class SessionOut(BaseModel):
    """`GET /api/me` — the session probe the app boots on. A 401 here is normal, not an error."""

    household: HouseholdOut
    counts: SessionCounts


class HouseholdPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    care_recipient_name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    # Length is checked in the handler, not here, so the 422 reads as a sentence a family
    # understands instead of pydantic's "String should have at least 12 characters".
    new_password: str


# --- members ------------------------------------------------------------------------------


class MemberOut(BaseModel):
    """A WhatsApp participant, not a login account: there is one shared family credential."""

    id: UUID
    display_name: str
    wa_jid: str | None = None
    wa_lid: str | None = None
    message_count: int = 0
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime


class MemberMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into_member_id: UUID
