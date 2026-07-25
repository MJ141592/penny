"""The extraction runner, entirely offline: FakeTransport replays hand-built Response JSON.

Nothing here touches the network. Every `Response` below was written by hand from the shape
the SDK parses, so these tests exercise our orchestration of a *parsed* response rather than
HTTP bytes — and they stay honest whether or not anyone has paid for a recorded eval run.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.extraction.chunker import ChunkMessage, build_chunks
from app.extraction.dedup import compute_dedup_key
from app.extraction.merge import DIFFERENT_EVENTS_MARKER, appointment_status, union_events
from app.extraction.runner import (
    EXTRACT_MAX_OUTPUT_TOKENS,
    MIN_CHUNK_MESSAGES,
    ExtractedEventRecord,
    ExtractionRunResult,
    OpenEvent,
    as_chunk_messages,
    extract_messages,
    local_message_id,
    occurred_at_utc,
    render_call_input,
)
from app.ingest.contract import InboundMessage
from app.llm.gateway import LLMGateway
from app.llm.prompts import EXTRACT_PROMPT, EXTRACT_PROMPT_VERSION, MERGE_PROMPT
from app.llm.schemas import ExtractedEvent
from app.llm.transport import FakeTransport
from scripts import eval_extraction, extract_export

TZ = ZoneInfo("Europe/London")
MODEL = "gpt-5.5-2026-04-23"
CARE_BRIEF = "Care recipient: Margaret Doyle, 84, called Mum in this chat."

# 5,600 in / 760 out at $5/$30 per Mtok — the plan's per-chunk cost, so the money assertions
# below are the same arithmetic the cost table was built from.
CHUNK_COST = Decimal("0.050800")


@pytest.fixture(autouse=True)
def _pinned_settings(settings_override: Callable[..., Settings]) -> None:
    """Never read the developer's .env: the model id decides the price arithmetic."""
    settings_override(llm_model_extract=MODEL, llm_model_report=MODEL, openai_api_key="test-key")


# --- builders ------------------------------------------------------------------------

_DIZZY = "Mum says she felt dizzy standing up in the kitchen this morning"
_TEXTS = {
    2: "cardiology is the 22nd at half two, I'll take her",
    3: "the appointment went fine, bloods taken, review in 2 weeks",
    6: "she had a bad night again, up 4 times",
    11: _DIZZY,
    # m12 of the SECOND half of a 100-message chunk, so the split-and-retry test has a
    # quotable message on both sides of the cut.
    61: _DIZZY,
}


def chunk_messages(count: int = 30, *, day: int = 0) -> list[ChunkMessage]:
    """A slice of conversation on one afternoon, tight enough to stay a single chunk."""
    start = datetime(2026, 7, 14, 12, 0, tzinfo=UTC) + timedelta(days=day)
    return [
        ChunkMessage(
            id=UUID(int=day * 1000 + index),
            sent_at=start + timedelta(minutes=index),
            sender_display_name="Sarah" if index % 2 else "Tom",
            text=_TEXTS.get(index, f"filler message {day}-{index}"),
            source_ordinal=day * 1000 + index,
        )
        for index in range(count)
    ]


def event(**overrides: Any) -> dict[str, Any]:
    """One `ExtractedEvent` with every field present — strict mode requires all of them."""
    base: dict[str, Any] = {
        "kind": "appointment",
        "natural_key": "cardiology salford royal",
        "title": "Cardiology appointment, Salford Royal",
        "body": "Sarah is taking Margaret to cardiology on the 22nd.",
        "occurred_at": "2026-07-22T14:30",
        "occurred_at_precision": "datetime",
        "date_basis": "the 22nd at half two",
        "is_future": True,
        "subject": "Margaret",
        "actors": ["Sarah"],
        "confidence": "high",
        "source_message_handles": ["m3"],
        "quotes": ["cardiology is the 22nd at half two"],
        "symptom_name": None,
        "severity": None,
        "body_site": None,
        "duration_text": None,
        "appointment_kind": "specialist",
        "provider_name": "Salford Royal cardiology",
        "location": "Salford Royal",
        "attendees": ["Sarah", "Margaret"],
        "outcome": None,
        "follow_up_actions": [],
        "medication_name": None,
        "dose_text": None,
        "medication_action": None,
        "prescriber": None,
        "note_category": None,
    }
    return base | overrides


def symptom(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "kind": "symptom",
        "natural_key": "dizziness",
        "title": "Dizzy spell in the kitchen",
        "body": None,
        "occurred_at": "2026-07-14",
        "occurred_at_precision": "date",
        "date_basis": "this morning",
        "is_future": False,
        "source_message_handles": ["m12"],
        "quotes": ["she felt dizzy standing up in the kitchen"],
        "appointment_kind": None,
        "provider_name": None,
        "location": None,
        "attendees": [],
        "symptom_name": "dizziness",
        "severity": "moderate",
    }
    return event(**(defaults | overrides))


def completed(*events: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    payload = json.dumps({"events": list(events), "no_events_reason": reason})
    return {
        "id": f"resp_{uuid4().hex[:8]}",
        "created_at": 1784275920.0,
        "model": MODEL,
        "object": "response",
        "output": [
            {
                "id": "msg_0001",
                "content": [{"annotations": [], "text": payload, "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
        "usage": _usage(),
    }


def merged_response(payload: dict[str, Any]) -> dict[str, Any]:
    """A merge call returns one bare `ExtractedEvent`, not an `ExtractionResult`."""
    response = completed()
    response["output"][0]["content"][0]["text"] = json.dumps(payload)
    return response


def incomplete(reason: str = "max_output_tokens") -> dict[str, Any]:
    response = completed()
    response["output"] = []
    response["status"] = "incomplete"
    response["incomplete_details"] = {"reason": reason}
    return response


def _usage() -> dict[str, Any]:
    return {
        "input_tokens": 5_600,
        "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
        "output_tokens": 760,
        "output_tokens_details": {"reasoning_tokens": 250},
        "total_tokens": 6_360,
    }


def gateway_over(*fixtures: dict[str, Any]) -> tuple[LLMGateway, FakeTransport]:
    transport = FakeTransport(list(fixtures))
    return LLMGateway(transport=transport), transport


async def run(
    messages: list[ChunkMessage],
    *fixtures: dict[str, Any],
    **kwargs: Any,
) -> tuple[Any, FakeTransport]:
    gateway, transport = gateway_over(*fixtures)
    result = await extract_messages(
        messages, care_brief=CARE_BRIEF, tz=TZ, gateway=gateway, **kwargs
    )
    return result, transport


# --- the happy path ------------------------------------------------------------------


async def test_one_chunk_produces_exact_events_dedup_keys_and_sources() -> None:
    messages = chunk_messages()
    result, transport = await run(messages, completed(event(), symptom()))

    assert len(transport.calls) == 1
    assert [record.event.title for record in result.events] == [
        "Dizzy spell in the kitchen",
        "Cardiology appointment, Salford Royal",
    ]

    appointment, dizzy = result.events[1], result.events[0]
    assert appointment.dedup_key == compute_dedup_key(appointment.event, TZ)
    assert dizzy.dedup_key == compute_dedup_key(dizzy.event, TZ)
    # m3 and m12 are 1-based handles over the chunk's primary messages.
    assert appointment.source_message_ids == [messages[2].id]
    assert dizzy.source_message_ids == [messages[11].id]
    assert appointment.occurred_at_utc == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert dizzy.occurred_at_utc == datetime(2026, 7, 14, tzinfo=UTC)
    assert result.extracted_message_ids == [m.id for m in messages]
    assert result.unextracted_message_ids == []
    assert result.total_cost_usd == CHUNK_COST
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (5_600, 760, 250)


async def test_quotes_are_attributed_to_the_message_that_contains_them() -> None:
    messages = chunk_messages()
    result, _ = await run(
        messages,
        completed(
            event(
                source_message_handles=["m3", "m7"],
                quotes=["she had a bad night again", "cardiology is the 22nd"],
            )
        ),
    )
    excerpts = result.events[0].source_excerpts
    assert [(e.message_id, e.sender) for e in excerpts] == [
        (messages[6].id, "Tom"),
        (messages[2].id, "Tom"),
    ]


async def test_request_carries_the_plan_s_extraction_budget_and_no_forbidden_params() -> None:
    _, transport = await run(chunk_messages(), completed(event()))
    call = transport.calls[0]

    assert call["instructions"] == EXTRACT_PROMPT
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == EXTRACT_MAX_OUTPUT_TOKENS == 8_000
    assert call["text"]["format"]["strict"] is True
    assert not {"temperature", "top_p", "max_tokens"} & set(call)
    assert '<transcript timezone="Europe/London"' in call["input"]
    assert CARE_BRIEF in call["input"]
    # A UUID in a prompt is the one thing the handle scheme exists to prevent.
    assert str(chunk_messages()[0].id) not in call["input"]


async def test_prompt_version_reaches_the_audit_trail() -> None:
    gateway, _ = gateway_over(completed(event()))
    await extract_messages(chunk_messages(), care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)
    assert gateway.recorder.records[0].prompt_version == EXTRACT_PROMPT_VERSION


# --- grounding -----------------------------------------------------------------------


async def test_unknown_handle_drops_the_event_without_failing_the_run() -> None:
    result, _ = await run(
        chunk_messages(),
        completed(event(source_message_handles=["m99"]), symptom()),
    )
    assert [record.event.kind for record in result.events] == ["symptom"]
    assert result.dropped_events == {"no_valid_handle": 1}
    assert result.chunks[0].status == "ok"
    assert result.invalid_handles == 1
    assert result.handles_cited == 2
    assert result.invalid_handle_rate == pytest.approx(0.5)


async def test_a_partly_invented_handle_list_is_pruned_not_dropped() -> None:
    messages = chunk_messages()
    result, _ = await run(messages, completed(event(source_message_handles=["m3", "m99"])))
    assert result.events[0].source_message_ids == [messages[2].id]
    assert result.invalid_handles == 1
    assert result.dropped_events == {}


async def test_context_only_messages_never_become_sources() -> None:
    history = chunk_messages(4)
    later = [
        ChunkMessage(
            id=UUID(int=9_000 + i),
            sent_at=datetime(2026, 7, 14, 15, 0, tzinfo=UTC) + timedelta(minutes=i),
            sender_display_name="Tom",
            text=f"later message {i}",
            source_ordinal=9_000 + i,
        )
        for i in range(10)
    ]
    result, transport = await run(
        later,
        completed(event(source_message_handles=["c1"], quotes=[])),
        already_extracted=history,
    )
    assert "[c1] context_only" in transport.calls[0]["input"]
    assert result.events == []
    assert result.dropped_events == {"context_only_source": 1}


async def test_a_date_far_outside_the_transcript_is_dropped() -> None:
    result, _ = await run(
        chunk_messages(),
        completed(event(occurred_at="2019-03-04", occurred_at_precision="date")),
    )
    assert result.events == []
    assert result.dropped_events == {"out_of_range_date": 1}


async def test_quotes_no_cited_message_contains_drop_the_event() -> None:
    result, _ = await run(
        chunk_messages(),
        completed(event(quotes=["the consultant said her heart is fine"])),
    )
    assert result.events == []
    assert result.dropped_events == {"invented_quotes": 1}


# --- merging across chunks -----------------------------------------------------------


def two_chunk_messages() -> list[ChunkMessage]:
    """Two groups 20 days apart, so the chunker's MAX_SPAN closes a chunk between them."""
    return chunk_messages(15) + chunk_messages(15, day=20)


async def test_cross_chunk_duplicate_collapses_onto_one_dedup_key() -> None:
    messages = two_chunk_messages()
    assert len(build_chunks(messages)) == 2

    result, _ = await run(
        messages,
        completed(symptom()),
        completed(symptom(source_message_handles=["m12"], quotes=[])),
    )
    assert len(result.events) == 1
    record = result.events[0]
    assert record.mention_count == 2
    assert record.source_message_ids == [messages[11].id, messages[26].id]
    assert record.merged_by_llm is False
    assert result.merge_calls == 0


async def test_an_appointment_that_gains_an_outcome_pays_for_a_merge() -> None:
    messages = two_chunk_messages()
    outcome_event = event(
        source_message_handles=["m4"],
        quotes=["the appointment went fine, bloods taken"],
        outcome="Bloods taken, review in 2 weeks",
        is_future=False,
    )  # m4 of the second chunk is the message reporting back
    merged = event(
        outcome="Bloods taken, review in 2 weeks",
        body="Sarah took Margaret to cardiology. Bloods taken, review in two weeks.",
        is_future=False,
        source_message_handles=["m3"],
    )
    result, transport = await run(
        messages,
        completed(event()),
        completed(outcome_event),
        merged_response(merged),
    )

    merge_call = transport.calls[2]
    assert merge_call["instructions"] == MERGE_PROMPT
    assert merge_call["reasoning"] == {"effort": "medium"}
    assert merge_call["max_output_tokens"] == 2_000
    assert result.merge_calls == 1

    record = result.events[0]
    assert record.event.outcome == "Bloods taken, review in 2 weeks"
    assert record.merged_by_llm is True
    assert record.source_message_ids == [messages[2].id, messages[18].id]  # m3, then m4
    # Handles are chunk-local; a merged event must never keep the model's version of them.
    assert record.event.source_message_handles == ["m3", "m4"]


async def test_different_events_marker_keeps_both_under_sibling_keys() -> None:
    messages = two_chunk_messages()
    outcome_event = event(
        source_message_handles=["m4"],
        quotes=["the appointment went fine, bloods taken"],
        outcome="Rebooked for August",
        is_future=False,
    )
    verdict = event(body=f"{DIFFERENT_EVENTS_MARKER} the March one was rescheduled, not attended")
    result, _ = await run(
        messages, completed(event()), completed(outcome_event), merged_response(verdict)
    )

    assert len(result.events) == 2
    keys = sorted(record.dedup_key for record in result.events)
    assert keys[1] == f"{keys[0]}#2"


async def test_the_digest_of_this_run_s_events_reaches_a_later_chunk() -> None:
    messages = [m for n in range(5) for m in chunk_messages(15, day=20 * n)]
    _, transport = await run(messages, *[completed(event()) for _ in range(5)])
    # Chunks 1-4 run as one batch and cannot see each other; chunk 5 sees all of them.
    assert "<open_events>" not in transport.calls[0]["input"]
    assert "[E1] appointment" in transport.calls[4]["input"]
    assert "Cardiology appointment, Salford Royal" in transport.calls[4]["input"]


# --- failure paths -------------------------------------------------------------------


async def test_a_truncated_chunk_splits_in_half_and_retries() -> None:
    messages = chunk_messages(100)
    result, transport = await run(
        messages,
        incomplete(),  # first attempt
        incomplete(),  # the gateway's doubled-budget retry -> ChunkTooLargeError
        completed(event()),  # first half
        completed(symptom()),  # second half
    )
    assert len(transport.calls) == 4
    assert transport.calls[2]["input"].count("\n[m") < transport.calls[0]["input"].count("\n[m")
    assert [chunk.label for chunk in result.chunks] == ["1.1", "1.2"]
    assert len(result.events) == 2
    assert result.extracted_message_ids == [m.id for m in messages]


async def test_splitting_stops_at_the_floor_and_records_the_failure() -> None:
    messages = chunk_messages(MIN_CHUNK_MESSAGES)
    result, transport = await run(messages, incomplete(), incomplete())

    assert len(transport.calls) == 2  # no third call: 30 messages is the floor
    assert [chunk.status for chunk in result.chunks] == ["failed"]
    assert result.chunks[0].error == "ChunkTooLargeError"
    assert result.events == []
    assert result.extracted_message_ids == []
    assert result.unextracted_message_ids == [m.id for m in messages]


async def test_a_content_filter_refusal_marks_the_messages_extracted() -> None:
    messages = chunk_messages()
    result, _ = await run(messages, incomplete("content_filter"))

    assert result.chunks[0].status == "filtered"
    assert result.events == []
    # Retrying the same text refuses again, so these must not come back round for ever.
    assert result.extracted_message_ids == [m.id for m in messages]
    assert result.unextracted_message_ids == []


async def test_the_spend_cap_aborts_and_leaves_the_rest_unextracted() -> None:
    messages = [m for n in range(5) for m in chunk_messages(15, day=20 * n)]
    result, transport = await run(
        messages,
        *[completed(event()) for _ in range(4)],
        max_spend_usd=Decimal("0.01"),
    )
    # The first batch of four is in flight before any of it has been billed; the fifth chunk
    # never starts, and its messages stay unextracted so the next run resumes from there.
    assert len(transport.calls) == 4
    assert result.aborted_reason == "spend_cap"
    assert result.chunks[-1].status == "skipped"
    assert result.unextracted_message_ids == [m.id for m in messages[60:]]
    assert len(result.extracted_message_ids) == 60


# --- pure helpers --------------------------------------------------------------------


def test_as_chunk_messages_drops_system_lines_and_mints_stable_ids() -> None:
    inbound = [
        InboundMessage(
            provider="whatsapp_export",
            provider_message_id=None,
            sender_wa_jid=None,
            sender_wa_lid=None,
            sender_display_name="Sarah",
            sent_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            text="Morning all",
            source_ordinal=0,
        ),
        InboundMessage(
            provider="whatsapp_export",
            provider_message_id=None,
            sender_wa_jid=None,
            sender_wa_lid=None,
            sender_display_name="Sarah",
            sent_at=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
            text="Sarah created group",
            message_type="system",
            source_ordinal=1,
        ),
    ]
    adapted = as_chunk_messages(inbound)
    assert [m.text for m in adapted] == ["Morning all"]
    assert adapted[0].id == local_message_id(inbound[0])
    assert as_chunk_messages(inbound)[0].id == adapted[0].id


@pytest.mark.parametrize(
    ("precision", "value", "expected"),
    [
        ("datetime", "2026-07-22T14:30", datetime(2026, 7, 22, 13, 30, tzinfo=UTC)),
        ("exact", "2026-07-22T14:30", datetime(2026, 7, 22, 13, 30, tzinfo=UTC)),
        # A day in BST must NOT be timezone-converted, or "Tuesday" files under Monday.
        ("date", "2026-07-22", datetime(2026, 7, 22, tzinfo=UTC)),
        ("day", "2026-07-22", datetime(2026, 7, 22, tzinfo=UTC)),
        ("week", "2026-07-22", datetime(2026, 7, 20, tzinfo=UTC)),
        ("month", "2026-07", datetime(2026, 7, 1, tzinfo=UTC)),
        ("unknown", None, None),
    ],
)
def test_occurred_at_utc_encodes_precision(
    precision: str, value: str | None, expected: datetime | None
) -> None:
    assert occurred_at_utc(value, precision, TZ) == expected


def test_render_call_input_lists_open_events_by_handle_never_by_id() -> None:
    chunk = build_chunks(chunk_messages(5))[0]
    text = render_call_input(
        chunk,
        care_brief=CARE_BRIEF,
        tz=TZ,
        open_events=[
            OpenEvent("appointment", "2026-07-22", "Cardiology, Salford Royal", "scheduled"),
            OpenEvent("medication", "undated", "Apixaban"),
        ],
    )
    assert "[E1] appointment 2026-07-22 Cardiology, Salford Royal — scheduled" in text
    assert "[E2] medication undated Apixaban" in text
    assert "[m1] Tue 2026-07-14 13:00 — Tom: filler message 0-0" in text


def test_union_never_deletes_and_prefers_the_more_specific_date() -> None:
    stored = ExtractedEvent.model_validate(
        event(occurred_at="2026-07-22", occurred_at_precision="date", attendees=["Sarah"])
    )
    incoming = ExtractedEvent.model_validate(
        event(
            occurred_at="2026-07-22T14:30",
            occurred_at_precision="datetime",
            attendees=["Margaret"],
            outcome="Bloods taken",
            follow_up_actions=["Book the review"],
            is_future=False,
            confidence="medium",
        )
    )
    merged = union_events(stored, incoming)
    assert merged.occurred_at == "2026-07-22T14:30"
    assert merged.occurred_at_precision == "datetime"
    assert merged.attendees == ["Sarah", "Margaret"]
    assert merged.outcome == "Bloods taken"
    assert merged.follow_up_actions == ["Book the review"]
    assert merged.is_future is False
    assert merged.confidence == "medium"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "scheduled"),
        ({"outcome": "Bloods taken"}, "attended"),
        ({"title": "Cardiology appointment cancelled"}, "cancelled"),
        ({"body": "She missed it, nobody could take her"}, "missed"),
    ],
)
def test_appointment_status_is_derived_never_asked_of_the_model(
    overrides: dict[str, Any], expected: str
) -> None:
    assert appointment_status(ExtractedEvent.model_validate(event(**overrides))) == expected


# --- the CLI and the eval scorer -----------------------------------------------------

EXPORT_LINES = "\n".join(
    f"14/07/2026, {12 + i // 60:02d}:{i % 60:02d} - "
    + ("Sarah" if i % 2 else "Tom")
    + f": message number {i}"
    for i in range(40)
)


def test_dry_run_prices_the_real_prompt_and_makes_no_api_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = tmp_path / "chat.txt"
    export.write_text(EXPORT_LINES + "\n", encoding="utf-8")

    # No transport is constructed at all: a dry run that could reach the network would be a
    # dry run nobody trusts, and this is the path the docstring tells people to start with.
    assert (
        extract_export.main([str(export), "--dayfirst", "--tz", "Europe/London", "--dry-run"]) == 0
    )

    printed = capsys.readouterr().out
    assert "Dry run — no API call was made." in printed
    assert "chunks           1" in printed
    assert "input tokens     ~" in printed
    assert "projected cost   ~$" in printed


def prediction(
    *,
    kind: str = "symptom",
    title: str = "Dizzy spell in the kitchen",
    occurred_at: str = "2026-01-19",
    precision: str = "date",
    sources: list[UUID] | None = None,
    **overrides: Any,
) -> ExtractedEventRecord:
    parsed = ExtractedEvent.model_validate(
        symptom(title=title, occurred_at=occurred_at, occurred_at_precision=precision, **overrides)
    )
    return ExtractedEventRecord(
        dedup_key=compute_dedup_key(parsed, TZ),
        event=parsed,
        source_message_ids=list(sources or [UUID(int=1)]),
        source_excerpts=[],
        occurred_at_utc=occurred_at_utc(occurred_at, precision, TZ),
    )


def label(**overrides: Any) -> eval_extraction.Label:
    defaults: dict[str, Any] = {
        "id": "gt-01",
        "kind": "symptom",
        "occurred_at": datetime(2026, 1, 19, tzinfo=UTC),
        "precision": "day",
        "title": "Dizzy spell standing up in the kitchen",
        "must_mention": ("dizz",),
        "source_line_numbers": (72, 80),
    }
    return eval_extraction.Label(**(defaults | overrides))


def scored(labels: list[Any], predictions: list[ExtractedEventRecord]) -> Any:
    result = ExtractionRunResult(events=predictions)
    return eval_extraction.score(labels, result, {72: UUID(int=1), 80: UUID(int=2)})


def test_scorer_separates_found_from_found_with_the_right_date() -> None:
    outcome = scored([label()], [prediction(occurred_at="2026-01-26")])
    assert outcome.matched == {"gt-01": 0}
    assert outcome.date_correct == set()
    assert outcome.invented == []


def test_scorer_counts_a_surviving_duplicate_pair() -> None:
    outcome = scored([label()], [prediction(), prediction(title="Another dizzy turn")])
    assert outcome.duplicate_pairs == 1
    assert outcome.date_correct == {"gt-01"}
    assert outcome.invented == []


def test_scorer_calls_an_event_invented_only_when_no_labelled_line_backs_it() -> None:
    outcome = scored(
        [label()],
        [
            prediction(),
            # Neither of these can be confused with the label: no "dizz" anywhere in them.
            prediction(
                title="Sore knee",
                natural_key="sore knee",
                symptom_name="knee pain",
                sources=[UUID(int=2)],  # a line the ground truth points at
            ),
            prediction(
                title="Sore back",
                natural_key="sore back",
                symptom_name="back pain",
                sources=[UUID(int=99)],  # filler only — nothing in the fixture says this
            ),
        ],
    )
    assert [outcome.predictions[i].event.title for i in outcome.invented] == ["Sore back"]
    assert [outcome.predictions[i].event.title for i in outcome.unmatched_but_grounded] == [
        "Sore knee"
    ]


@pytest.mark.parametrize(
    ("label_precision", "label_day", "predicted", "predicted_precision", "correct"),
    [
        ("week", 16, "2026-03-18", "date", True),
        ("week", 16, "2026-03-24", "date", False),
        ("month", 1, "2026-03-29", "date", True),
        ("day", 16, "2026-03-16", "date", True),
    ],
)
def test_scorer_tolerance_follows_the_label_s_own_precision(
    label_precision: str, label_day: int, predicted: str, predicted_precision: str, correct: bool
) -> None:
    outcome = scored(
        [
            label(
                precision=label_precision,
                occurred_at=datetime(2026, 3, label_day, tzinfo=UTC),
            )
        ],
        [prediction(occurred_at=predicted, precision=predicted_precision)],
    )
    assert ("gt-01" in outcome.date_correct) is correct


def test_acceptance_thresholds_are_the_plan_s_and_are_not_softened() -> None:
    assert (eval_extraction.MIN_EVENTS_FOUND, eval_extraction.MAX_INVENTED) == (20, 2)
    assert eval_extraction.MAX_DUPLICATE_PAIRS == 2
    passing = eval_extraction.Score(labels=[], predictions=[])
    passing.date_correct = {f"gt-{i:02d}" for i in range(1, 21)}
    assert passing.passed is True
    passing.date_correct.pop()
    assert passing.passed is False


def test_ground_truth_line_numbers_all_resolve_to_real_messages() -> None:
    """The eval's only bridge between labelled lines and extracted events. If the alignment
    walk drifts, every 'invented event' verdict silently becomes noise."""
    _, by_line = eval_extraction.load_export()
    lines = {line for item in eval_extraction.load_labels() for line in item.source_line_numbers}
    assert lines and lines <= set(by_line)
