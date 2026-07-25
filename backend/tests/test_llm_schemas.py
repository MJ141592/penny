"""The schema is the contract with OpenAI. Pin it, or it breaks in production instead of CI."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.llm.pricing import PRICES, UnpricedModelError, cost_usd
from app.llm.prompts import (
    EXTRACT_PROMPT,
    EXTRACT_PROMPT_VERSION,
    MERGE_PROMPT,
    MERGE_PROMPT_VERSION,
)
from app.llm.schemas import (
    ExtractedEvent,
    ExtractionResult,
    to_strict_json_schema,
    validate_against_transcript,
)

GOLDEN_SCHEMA = Path(__file__).parent / "fixtures" / "llm" / "extraction_result.schema.json"


def _event(**overrides: Any) -> ExtractedEvent:
    base: dict[str, Any] = {
        "kind": "symptom",
        "natural_key": "poor sleep",
        "title": "Poor sleep, up 4 times overnight",
        "body": "Margaret was awake four times.",
        "occurred_at": "2026-07-14",
        "occurred_at_precision": "date",
        "date_basis": "last night",
        "is_future": False,
        "subject": "Margaret",
        "actors": ["Sarah"],
        "confidence": "high",
        "source_message_handles": ["m17"],
        "quotes": ["she had a bad night again, up 4 times"],
        "symptom_name": "poor sleep",
        "severity": "moderate",
        "body_site": None,
        "duration_text": None,
        "appointment_kind": None,
        "provider_name": None,
        "location": None,
        "attendees": [],
        "outcome": None,
        "follow_up_actions": [],
        "medication_name": None,
        "dose_text": None,
        "medication_action": None,
        "prescriber": None,
        "note_category": None,
    }
    return ExtractedEvent.model_validate(base | overrides)


# --- the strict schema -------------------------------------------------------------------


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found += _objects(item)
    elif isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found += _objects(value)
    return found


def test_strict_schema_matches_the_golden_file() -> None:
    """A field rename here is a 400 from OpenAI that fails the whole pipeline. Deliberate only."""
    generated = to_strict_json_schema(ExtractionResult)
    assert generated == json.loads(GOLDEN_SCHEMA.read_text())


def test_every_object_forbids_extra_properties_and_requires_all_of_them() -> None:
    schema = to_strict_json_schema(ExtractionResult)
    objects = _objects(schema)
    assert len(objects) == 2  # ExtractionResult and the ExtractedEvent $def
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_schema_carries_no_length_or_pattern_keywords() -> None:
    """Constraints live in `description` and in our validators — strict mode rejects them here."""
    blob = json.dumps(to_strict_json_schema(ExtractionResult))
    for keyword in ("maxLength", "minLength", "pattern", "minItems", "maxItems", "format"):
        assert f'"{keyword}"' not in blob


def test_nullable_fields_are_anyof_not_omitted() -> None:
    event = to_strict_json_schema(ExtractionResult)["$defs"]["ExtractedEvent"]
    assert {"type": "null"} in event["properties"]["body"]["anyOf"]
    assert "occurred_at" in event["required"]


# --- our own enforcement -----------------------------------------------------------------


def test_fields_of_other_kinds_are_nulled_out() -> None:
    """A stray provider_name on a symptom becomes a wrong `details` entry in the API."""
    event = _event(
        kind="symptom", provider_name="Dr Aziz", attendees=["Sarah"], note_category="mood"
    )
    assert event.provider_name is None
    assert event.attendees == []
    assert event.note_category is None
    assert event.symptom_name == "poor sleep"


def test_lengths_are_clamped_and_the_trailing_period_stripped() -> None:
    event = _event(title="x" * 200 + ".", body="y" * 900, quotes=["z" * 900])
    assert len(event.title) == 80
    assert not event.title.endswith(".")
    assert len(event.body) == 400
    assert len(event.quotes[0]) == 200


def test_events_citing_only_unknown_handles_are_dropped() -> None:
    kept = validate_against_transcript(
        [_event(source_message_handles=["m99"]), _event(source_message_handles=["m99", "m17"])],
        {"m17": "she had a bad night again, up 4 times"},
    )
    assert len(kept) == 1
    assert kept[0].source_message_handles == ["m17"]


def test_quotes_are_matched_case_and_whitespace_folded() -> None:
    kept = validate_against_transcript(
        [_event(quotes=["She Had  a bad\nnight again"])],
        {"m17": "she had a bad night again, up 4 times"},
    )
    assert kept[0].quotes == ["She Had  a bad\nnight again"]


def test_an_event_whose_every_quote_was_invented_is_dropped() -> None:
    kept = validate_against_transcript(
        [_event(quotes=["the doctor said it was a stroke"])],
        {"m17": "she had a bad night again, up 4 times"},
    )
    assert kept == []


def test_a_single_invented_quote_is_dropped_but_the_event_survives() -> None:
    kept = validate_against_transcript(
        [_event(quotes=["up 4 times", "the doctor said it was a stroke"])],
        {"m17": "she had a bad night again, up 4 times"},
    )
    assert kept[0].quotes == ["up 4 times"]


def test_extraction_result_accepts_an_empty_run() -> None:
    result = ExtractionResult.model_validate({"events": [], "no_events_reason": "Only chit-chat."})
    assert result.events == []


# --- pricing -----------------------------------------------------------------------------


def test_both_configured_models_are_priced() -> None:
    """An unpriced model cannot ship: every spend guard downstream would silently read zero."""
    settings = get_settings()
    assert settings.llm_model_extract in PRICES
    assert settings.llm_model_report in PRICES


def test_cost_of_a_typical_extraction_chunk() -> None:
    # 5,600 in + 760 out on gpt-5.5 is the per-chunk figure the whole cost model rests on.
    assert cost_usd("gpt-5.5-2026-04-23", 5_600, 760) == Decimal("0.0508")


def test_cached_input_bills_at_a_tenth() -> None:
    full = cost_usd("gpt-5.5-2026-04-23", 1_000_000, 0)
    cached = cost_usd("gpt-5.5-2026-04-23", 1_000_000, 0, cached_input_tokens=1_000_000)
    assert full == Decimal("5")
    assert cached == Decimal("0.5")


def test_an_unpriced_model_raises_rather_than_billing_zero() -> None:
    with pytest.raises(UnpricedModelError):
        cost_usd("gpt-4o-mini", 100, 100)


# --- prompts -----------------------------------------------------------------------------


def test_prompt_versions_are_pinned() -> None:
    """These hashes ARE the version recorded in llm_runs. Changing a prompt changes them,
    which is the point: update the constant in the same commit as the prompt edit."""
    assert EXTRACT_PROMPT_VERSION == "f05401d5cccf"
    assert MERGE_PROMPT_VERSION == "bb1264da8799"


def test_extract_prompt_states_the_rules_that_cost_us_money_when_forgotten() -> None:
    for phrase in (
        "DO NOT EXTRACT",
        "DATES",
        "THREADS",
        "NATURAL KEY",
        "CONFIDENCE",
        "context_only",
    ):
        assert phrase in EXTRACT_PROMPT
    # Roughly 1,100-1,300 tokens: the fixed overhead every chunk pays for.
    assert 4_000 < len(EXTRACT_PROMPT) < 6_000


def test_merge_prompt_forbids_losing_information() -> None:
    assert "Never delete information" in MERGE_PROMPT
    assert "DIFFERENT EVENTS:" in MERGE_PROMPT
