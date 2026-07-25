"""What the extraction call returns, and the strict JSON Schema we send to get it.

ONE FLAT MODEL, not a discriminated union. OpenAI strict structured outputs reject
`discriminator` and reject a non-object root, so every kind-specific field is present on
every event and nulled out for the kinds it does not belong to. The API layer folds these
flat fields into the per-kind `details` object from docs/api-contract.md; the browser never
sees this shape.

Length and shape rules live in `Field(description=...)` and are enforced by OUR validators,
never as JSON Schema keywords. Strict structured outputs have historically rejected
`maxLength`/`pattern`/`minItems`, and a 400 on schema validation fails the entire pipeline
rather than one event. Descriptions steer the model; the validators make it true.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal["symptom", "appointment", "medication", "note"]
Precision = Literal["datetime", "date", "week", "month", "unknown"]
Confidence = Literal["high", "medium", "low"]
Severity = Literal["mild", "moderate", "severe", "unknown"]
ApptKind = Literal["gp", "specialist", "hospital", "test", "therapy", "other"]
MedAction = Literal["started", "stopped", "changed", "missed", "refilled", "side_effect", "other"]
NoteCat = Literal["logistics", "mood", "finance", "equipment", "admin", "other"]

TITLE_MAX_CHARS = 80
BODY_MAX_CHARS = 400
QUOTE_MAX_CHARS = 200

# Which flat fields belong to which kind. Anything not owned by `kind` is nulled after
# parsing: models happily fill in `provider_name` on a symptom, and a stray value there
# becomes a wrong `details` entry the moment the API folds the flat model into the union.
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "symptom": ("symptom_name", "severity", "body_site", "duration_text"),
    "appointment": (
        "appointment_kind",
        "provider_name",
        "location",
        "attendees",
        "outcome",
        "follow_up_actions",
    ),
    "medication": ("medication_name", "dose_text", "medication_action", "prescriber"),
    "note": ("note_category",),
}
_LIST_FIELDS = frozenset({"attendees", "follow_up_actions"})


class ExtractedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- common ---
    kind: Kind
    natural_key: str = Field(
        description=(
            "A short lowercase phrase that would identify this same thing if the family "
            "mentioned it again next week, e.g. 'cardiology salford royal', 'apixaban started', "
            "'dizziness'. Do not hash it, do not add dates, do not invent an id."
        )
    )
    title: str = Field(
        description=(
            "At most 80 characters. No trailing full stop. "
            "E.g. 'Cardiology appointment, Salford Royal'."
        )
    )
    body: str | None = Field(
        description=(
            "At most 400 characters. Third person, factual, only what the messages state. "
            "Null if the title says everything."
        )
    )
    occurred_at: str | None = Field(
        description=(
            "'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' in the household timezone. Null if undeterminable."
        )
    )
    occurred_at_precision: Precision
    date_basis: str | None = Field(
        description="The verbatim phrase the date came from, e.g. 'last Tuesday', 'on the 17th'."
    )
    is_future: bool = Field(
        description=(
            "True if the event had not happened yet at the time of the message describing it."
        )
    )
    subject: str | None = Field(
        description="Name of the person the event is ABOUT, taken from the care brief."
    )
    actors: list[str] = Field(
        description="Names of the people who did or reported it. Empty list if unclear."
    )
    confidence: Confidence
    source_message_handles: list[str] = Field(
        description=(
            "Chunk-local message handles like 'm17', exactly as printed in the transcript. "
            "Never invent one, never cite a context_only line."
        )
    )
    quotes: list[str] = Field(
        description=(
            "Short verbatim excerpts, at most 200 characters each, copied character for "
            "character from a message you cited. Never paraphrase inside a quote."
        )
    )

    # --- symptom (null unless kind == 'symptom') ---
    symptom_name: str | None
    severity: Severity | None
    body_site: str | None
    duration_text: str | None

    # --- appointment (null unless kind == 'appointment') ---
    appointment_kind: ApptKind | None
    provider_name: str | None
    location: str | None
    attendees: list[str]
    outcome: str | None = Field(
        description="Only if the messages describe what actually HAPPENED at the appointment."
    )
    follow_up_actions: list[str]

    # --- medication (null unless kind == 'medication') ---
    medication_name: str | None
    dose_text: str | None
    medication_action: MedAction | None
    prescriber: str | None

    # --- note (null unless kind == 'note') ---
    note_category: NoteCat | None

    @model_validator(mode="after")
    def _enforce_shape(self) -> ExtractedEvent:
        """Apply the rules we deliberately kept out of the JSON Schema."""
        for kind, fields in _KIND_FIELDS.items():
            if kind == self.kind:
                continue
            for name in fields:
                setattr(self, name, [] if name in _LIST_FIELDS else None)

        self.title = self.title.strip()[:TITLE_MAX_CHARS].rstrip().rstrip(".")
        if self.body is not None:
            self.body = self.body.strip()[:BODY_MAX_CHARS].rstrip()
        # Truncating a quote keeps it a prefix of the original, so it still passes the
        # substring check in validate_against_transcript.
        self.quotes = [q.strip()[:QUOTE_MAX_CHARS].rstrip() for q in self.quotes if q.strip()]
        return self


# Object root: strict structured outputs reject an array or a scalar at the top level.
# Kept as a comment rather than a docstring — pydantic copies a docstring into the schema's
# `description`, and that description is sent to the model on every single call.
class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ExtractedEvent]
    no_events_reason: str | None = Field(
        description="One line explaining why nothing qualified. Null when events is non-empty."
    )


def _fold(text: str) -> str:
    """Case- and whitespace-insensitive form, so a quote survives NBSPs and copy drift."""
    normalised = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalised).strip().casefold()


def validate_against_transcript(
    events: list[ExtractedEvent],
    handle_to_text: dict[str, str],
) -> list[ExtractedEvent]:
    """Drop what the transcript does not support. Grounding check, not a schema check.

    Takes the message text as an argument rather than reading it itself so the model layer
    stays IO-free and this stays a pure function the extraction runner can call twice.

    Two rules, both about hallucination:
      * an event citing no real handle has no evidence at all -> drop the event;
      * a quote that is not a folded substring of a cited message was invented -> drop the
        quote, and drop the whole event if that was its only quote, because an event whose
        every quote was fabricated is not one we will show a family as verbatim evidence.
    """
    kept: list[ExtractedEvent] = []
    for event in events:
        handles = [h for h in event.source_message_handles if h in handle_to_text]
        if not handles:
            continue
        cited = _fold(" \n ".join(handle_to_text[h] for h in handles))
        quotes = [q for q in event.quotes if _fold(q) and _fold(q) in cited]
        if event.quotes and not quotes:
            continue
        event.source_message_handles = handles
        event.quotes = quotes
        kept.append(event)
    return kept


def to_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's schema, tightened to what OpenAI strict structured outputs demand.

    Generated here rather than imported from the SDK's private `openai.lib._pydantic`:
    the exact bytes we send are snapshot-tested, and a private helper moving under an SDK
    bump would break extraction in production instead of in CI.
    """
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _tighten(item)
        return
    if not isinstance(node, dict):
        return
    # Strict mode forbids `default` and requires every property to be listed in `required`
    # — optionality is expressed as anyOf[T, null], never by omission.
    node.pop("default", None)
    if node.get("type") == "object":
        node["additionalProperties"] = False
        node["required"] = list(node.get("properties", {}))
    for value in node.values():
        _tighten(value)
