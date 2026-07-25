"""What to do when two extractions land on the same `dedup_key`.

`dedup.decide_merge` says WHETHER a collision is worth money; this module carries it out.
The overwhelming majority of collisions are a family mentioning the same thing twice and are
resolved by `union_events` for free. Only an appointment that has gained an outcome, or a
medication whose dose changed, pays for a model call.

ONE RULE ABOVE ALL: never delete information. The union of both records is the answer. A
merge that shortens the evidence is a bug — the family sees the quotes, and a card that loses
the message it was built from loses the trust it was built for.

HANDLES DO NOT SURVIVE A MERGE. `source_message_handles` are chunk-local: `m3` in chunk 1 and
`m3` in chunk 7 are different messages. The runner has already resolved both sides to real
message ids before it gets here, so the merged event's handle list is rebuilt deterministically
and the model's version of it is discarded — trusting it would attribute one chunk's quote to
another chunk's message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.extraction.dedup import MergeDecision, decide_merge
from app.llm.gateway import CallSpec
from app.llm.prompts import MERGE_PROMPT, MERGE_PROMPT_VERSION
from app.llm.schemas import ExtractedEvent

if TYPE_CHECKING:
    from decimal import Decimal
    from uuid import UUID

    from app.llm.gateway import LLMGateway

MERGE_MAX_OUTPUT_TOKENS = 2_000
MERGE_EFFORT = "medium"

# The model is told to lead with this when the two records are not actually the same event —
# a rescheduling that left the original standing, or a first visit and its follow-up.
DIFFERENT_EVENTS_MARKER = "DIFFERENT EVENTS:"

# Finer beats coarser: "2026-07-17T14:30" is a better answer than "2026-07-17", which is a
# better answer than "next week". `datetime`/`date` are the LLM schema's spellings of the
# contract's `exact`/`day`; both are accepted because docs/api-contract.md uses the other pair.
_PRECISION_RANK = {
    "unknown": 0,
    "month": 1,
    "week": 2,
    "date": 3,
    "day": 3,
    "datetime": 4,
    "exact": 4,
}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

_LIST_FIELDS = ("actors", "attendees", "follow_up_actions", "quotes", "source_message_handles")
# Everything the union fills in from `incoming` only when `existing` left it null.
_SCALAR_FIELDS = (
    "date_basis",
    "subject",
    "symptom_name",
    "severity",
    "body_site",
    "duration_text",
    "appointment_kind",
    "provider_name",
    "location",
    "outcome",
    "medication_name",
    "dose_text",
    "medication_action",
    "prescriber",
    "note_category",
)


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """The merged event, plus what it cost and whether it should have been merged at all."""

    event: ExtractedEvent
    decision: MergeDecision
    # True when the model said DIFFERENT EVENTS. The runner then stores `incoming` under a
    # sibling key instead of collapsing it, and `event` is the incoming record unchanged.
    separate: bool = False
    used_llm: bool = False
    cost_usd: Decimal | None = None
    run_id: UUID | None = None


def as_stored_view(event: ExtractedEvent) -> dict[str, Any]:
    """Project a flat `ExtractedEvent` into the stored/API shape `decide_merge` reads.

    `decide_merge` deliberately takes the row as the UI sees it — `kind` and `edited_at` at the
    top level, kind-specific fields under `details` — so one policy serves both the M1 runner
    (which has no database) and the M4 upsert (which reads a real row). This is the adapter.
    """
    return {
        "kind": event.kind,
        "edited_at": None,
        "occurred_at": event.occurred_at,
        "occurred_at_precision": event.occurred_at_precision,
        "title": event.title,
        "body": event.body,
        "details": _details(event),
    }


def _details(event: ExtractedEvent) -> dict[str, Any]:
    """The per-kind `details` object from docs/api-contract.md. Names differ from the flat
    model where the flat model needed a kind prefix: symptom_name -> symptom, and so on."""
    if event.kind == "symptom":
        return {
            "symptom": event.symptom_name,
            "severity": event.severity or "unknown",
            "body_site": event.body_site,
            "duration_text": event.duration_text,
        }
    if event.kind == "appointment":
        return {
            "appointment_kind": event.appointment_kind or "other",
            "provider_name": event.provider_name,
            "location": event.location,
            "attendees": list(event.attendees),
            "outcome": event.outcome,
            "follow_up_actions": list(event.follow_up_actions),
            "status": appointment_status(event),
        }
    if event.kind == "medication":
        return {
            "medication_name": event.medication_name,
            "dose_text": event.dose_text,
            "action": event.medication_action or "other",
            "prescriber": event.prescriber,
        }
    return {"category": event.note_category or "other"}


def appointment_status(event: ExtractedEvent) -> str:
    """`scheduled` until something says otherwise; an outcome is what says otherwise.

    The flat model has no `status` field on purpose — status is a fact about the event's life,
    derived from whether the family has reported back yet, and asking the model for it invites
    it to mark an appointment `attended` from the message that booked it.
    """
    text = f"{event.title} {event.body or ''}".casefold()
    if "cancel" in text:
        return "cancelled"
    if "missed" in text or "didn't go" in text or "did not go" in text:
        return "missed"
    if event.outcome:
        return "attended"
    return "scheduled"


def union_events(existing: ExtractedEvent, incoming: ExtractedEvent) -> ExtractedEvent:
    """Merge two records with no model call. Additive by construction: nothing is ever unset.

    `existing` owns identity — title and natural_key — so a second mention cannot rename an
    event the family has already seen. Everything else is a union.
    """
    merged = existing.model_dump()
    incoming_data = incoming.model_dump()

    for name in _SCALAR_FIELDS:
        if merged.get(name) in (None, "") and incoming_data.get(name) not in (None, ""):
            merged[name] = incoming_data[name]

    for name in _LIST_FIELDS:
        merged[name] = _ordered_union(merged.get(name) or [], incoming_data.get(name) or [])

    if _rank(incoming.occurred_at_precision) > _rank(existing.occurred_at_precision):
        merged["occurred_at"] = incoming.occurred_at
        merged["occurred_at_precision"] = incoming.occurred_at_precision
        merged["date_basis"] = incoming.date_basis or existing.date_basis
    elif existing.occurred_at is None and incoming.occurred_at is not None:
        merged["occurred_at"] = incoming.occurred_at
        merged["occurred_at_precision"] = incoming.occurred_at_precision

    # Once either record knows the event has happened, it has happened.
    merged["is_future"] = existing.is_future and incoming.is_future
    merged["confidence"] = min(
        (existing.confidence, incoming.confidence), key=lambda c: _CONFIDENCE_RANK[c]
    )
    # The longer body is the one carrying the outcome; there is no free way to narrate a
    # difference, and dropping the longer text would be exactly the deletion we forbid.
    if incoming.body and len(incoming.body) > len(existing.body or ""):
        merged["body"] = incoming.body

    return ExtractedEvent.model_validate(merged)


async def merge_events(
    existing: ExtractedEvent,
    incoming: ExtractedEvent,
    *,
    gateway: LLMGateway,
    household_id: UUID | None = None,
    model: str | None = None,
) -> MergeOutcome:
    """Resolve a `dedup_key` collision, paying for a model call only when one is warranted."""
    decision = decide_merge(as_stored_view(existing), incoming)
    if decision is MergeDecision.NO_CHANGE:
        return MergeOutcome(event=existing, decision=decision)
    if decision is MergeDecision.APPEND_SOURCES:
        return MergeOutcome(event=union_events(existing, incoming), decision=decision)

    result = await gateway.structured(
        CallSpec(
            purpose="merge",
            instructions=MERGE_PROMPT,
            input=(
                f"<existing>{existing.model_dump_json()}</existing>\n"
                f"<new>{incoming.model_dump_json()}</new>"
            ),
            schema=ExtractedEvent,
            max_output_tokens=MERGE_MAX_OUTPUT_TOKENS,
            reasoning_effort=MERGE_EFFORT,
            household_id=household_id,
            model=model,
            prompt_version=MERGE_PROMPT_VERSION,
        )
    )
    merged = result.parsed
    if (merged.body or "").lstrip().upper().startswith(DIFFERENT_EVENTS_MARKER):
        return MergeOutcome(
            event=incoming,
            decision=decision,
            separate=True,
            used_llm=True,
            cost_usd=result.cost_usd,
            run_id=result.run_id,
        )

    # See the module docstring: the model's handle list spans two chunks' namespaces and is
    # meaningless. Rebuild both evidence lists from the inputs the runner already resolved.
    merged.source_message_handles = _ordered_union(
        existing.source_message_handles, incoming.source_message_handles
    )
    merged.quotes = _ordered_union(existing.quotes, incoming.quotes)
    return MergeOutcome(
        event=merged,
        decision=decision,
        used_llm=True,
        cost_usd=result.cost_usd,
        run_id=result.run_id,
    )


def _ordered_union(first: list[str], second: list[str]) -> list[str]:
    seen = list(first)
    seen.extend(item for item in second if item not in seen)
    return seen


def _rank(precision: str) -> int:
    return _PRECISION_RANK.get(precision, 0)
