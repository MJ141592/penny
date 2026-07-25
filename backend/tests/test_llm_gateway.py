"""Gateway behaviour, entirely offline: FakeTransport replays recorded Response JSON.

Nothing here touches the network. The one real call in the project is the `live`-marked
smoke test, run deliberately with a spend cap.
"""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from app.config import Settings
from app.llm.gateway import (
    MAX_ATTEMPTS,
    BudgetExceededError,
    BudgetGuard,
    CallSpec,
    ChunkTooLargeError,
    ContentFilteredError,
    IncompleteResponseError,
    InMemoryRunRecorder,
    LLMGateway,
    ResponseParseError,
)
from app.llm.prompts import EXTRACT_PROMPT, EXTRACT_PROMPT_VERSION
from app.llm.schemas import ExtractionResult
from app.llm.transport import FakeTransport

FIXTURES = Path(__file__).parent / "fixtures" / "responses"
HOUSEHOLD = UUID("0d4f8b2e-1c8a-4c1e-9f6a-3b7d2e5a91cc")
MODEL = "gpt-5.5-2026-04-23"


def fixture(name: str, **overrides: Any) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text()) | overrides


def spec(**overrides: Any) -> CallSpec[ExtractionResult]:
    defaults: dict[str, Any] = {
        "purpose": "extract",
        "instructions": EXTRACT_PROMPT,
        "input": "<transcript>[m17] Tue 2026-07-14 21:04 - Sarah: cardiology is the 22nd</transcript>",
        "schema": ExtractionResult,
        "max_output_tokens": 4_000,
        "reasoning_effort": "low",
        "household_id": HOUSEHOLD,
        "prompt_version": EXTRACT_PROMPT_VERSION,
    }
    return CallSpec(**(defaults | overrides))


def http_error(
    cls: type[Exception], status: int, headers: dict[str, str] | None = None
) -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls("upstream said no", response=response, body=None)


@pytest.fixture(autouse=True)
def _pinned_settings(settings_override: Callable[..., Settings]) -> None:
    """Never read the developer's .env: the model id decides the price arithmetic below."""
    settings_override(llm_model_extract=MODEL, llm_model_report=MODEL, openai_api_key="test-key")


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def gateway(sleeps: list[float]) -> Callable[..., LLMGateway]:
    def _build(fixtures: list[Any], budget: BudgetGuard | None = None) -> LLMGateway:
        async def _record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return LLMGateway(
            transport=FakeTransport(fixtures),
            recorder=InMemoryRunRecorder(),
            budget=budget,
            sleep=_record_sleep,
        )

    return _build


# --- the call itself ---------------------------------------------------------------------


async def test_a_successful_call_parses_and_records(gateway: Callable[..., LLMGateway]) -> None:
    llm = gateway([fixture("extract_completed")])
    result = await llm.structured(spec())

    assert isinstance(result.parsed, ExtractionResult)
    assert result.parsed.events[0].natural_key == "cardiology salford royal"
    assert result.attempts == 1
    assert result.response_id == "resp_penny_completed"

    (record,) = llm.recorder.records
    assert record.status == "ok"
    assert record.purpose == "extract"
    assert record.prompt_version == EXTRACT_PROMPT_VERSION
    assert record.household_id == HOUSEHOLD


async def test_the_request_has_the_responses_api_structured_output_shape(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_completed")])
    await llm.structured(spec())
    (call,) = llm.transport.calls

    text_format = call["text"]["format"]
    assert text_format["type"] == "json_schema"  # NOT the Chat Completions response_format shape
    assert text_format["name"] == "ExtractionResult"
    assert text_format["strict"] is True
    assert text_format["schema"]["additionalProperties"] is False
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 4_000
    assert call["store"] is False  # family health chat is not retained server-side


async def test_forbidden_parameters_are_never_sent(gateway: Callable[..., LLMGateway]) -> None:
    """gpt-5.5 rejects these outright, and CallSpec cannot even express the first two."""
    llm = gateway([fixture("extract_completed"), fixture("extract_no_events")])
    await llm.structured(spec())
    await llm.structured(spec(reasoning_effort="none", max_output_tokens=800))

    for call in llm.transport.calls:
        assert "temperature" not in call
        assert "top_p" not in call
        assert "max_tokens" not in call


async def test_safety_identifier_is_a_hash_not_the_household_id(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_completed")])
    await llm.structured(spec())
    identifier = llm.transport.calls[0]["safety_identifier"]  # type: ignore[attr-defined]

    assert len(identifier) == 32
    assert str(HOUSEHOLD) not in identifier


async def test_no_household_means_no_safety_identifier(gateway: Callable[..., LLMGateway]) -> None:
    """Omitted entirely, not sent as null. The CLI and the eval have no household, and an
    explicit null is a value the API is entitled to reject on every call of a paid run."""
    llm = gateway([fixture("extract_completed")])
    await llm.structured(spec(household_id=None))
    assert "safety_identifier" not in llm.transport.calls[0]  # type: ignore[attr-defined]


def test_max_output_tokens_has_no_default_anywhere() -> None:
    """One call at the 128,000 ceiling costs $3.84. It must be an explicit decision."""
    with pytest.raises(TypeError):
        CallSpec(  # type: ignore[call-arg]
            purpose="extract", instructions="x", input="y", schema=ExtractionResult
        )


# --- retries -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        http_error(InternalServerError, 500),
        http_error(RateLimitError, 429),
        APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
        APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
    ],
    ids=["500", "429", "connection", "timeout"],
)
async def test_transient_failures_retry_then_succeed(
    gateway: Callable[..., LLMGateway], sleeps: list[float], error: Exception
) -> None:
    llm = gateway([error, fixture("extract_completed")])
    result = await llm.structured(spec())

    assert result.attempts == 2
    assert len(sleeps) == 1
    assert 0.75 <= sleeps[0] <= 1.15
    (record,) = llm.recorder.records  # one row per call, carrying the attempt count
    assert record.status == "ok"
    assert record.attempts == 2


async def test_retry_after_overrides_our_backoff(
    gateway: Callable[..., LLMGateway], sleeps: list[float]
) -> None:
    llm = gateway(
        [http_error(RateLimitError, 429, {"retry-after": "3"}), fixture("extract_completed")]
    )
    await llm.structured(spec())
    assert sleeps == [3.0]


async def test_a_400_never_retries(gateway: Callable[..., LLMGateway], sleeps: list[float]) -> None:
    """A BadRequestError is a schema or parameter bug: retrying burns money and hides it."""
    llm = gateway([http_error(BadRequestError, 400), fixture("extract_completed")])

    with pytest.raises(BadRequestError):
        await llm.structured(spec())

    assert len(llm.transport.calls) == 1
    assert sleeps == []
    (record,) = llm.recorder.records
    assert record.status == "error"
    assert record.error_class == "BadRequestError"
    assert record.attempts == 1


async def test_exhausted_retries_raise_the_real_error_and_record_it(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([http_error(InternalServerError, 500) for _ in range(MAX_ATTEMPTS)])

    with pytest.raises(InternalServerError):
        await llm.structured(spec())

    (record,) = llm.recorder.records
    assert (record.status, record.attempts, record.error_class) == (
        "error",
        MAX_ATTEMPTS,
        "InternalServerError",
    )
    assert record.cost_usd == Decimal("0")  # a failed run with zero tokens is still audit


# --- incomplete responses ----------------------------------------------------------------


async def test_truncation_retries_once_with_double_budget_and_lower_effort(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_incomplete_max_output_tokens"), fixture("extract_completed")])
    result = await llm.structured(spec(max_output_tokens=4_000, reasoning_effort="low"))

    first, second = llm.transport.calls
    assert first["max_output_tokens"] == 4_000
    assert second["max_output_tokens"] == 8_000
    # Reasoning tokens are the usual culprit and bill at the output rate.
    assert second["reasoning"] == {"effort": "none"}
    assert result.parsed.events

    truncated, retried = llm.recorder.records
    assert truncated.status == "incomplete"
    assert truncated.incomplete_reason == "max_output_tokens"
    # Tokens burned on a truncated response are still billed and still recorded.
    assert truncated.cost_usd > Decimal("0")
    assert retried.status == "ok"


async def test_truncated_twice_raises_chunk_too_large(gateway: Callable[..., LLMGateway]) -> None:
    """The runner catches this, splits the chunk in half and recurses."""
    llm = gateway([fixture("extract_incomplete_max_output_tokens")] * 2)

    with pytest.raises(ChunkTooLargeError) as excinfo:
        await llm.structured(spec())

    assert len(llm.transport.calls) == 2
    assert [r.status for r in llm.recorder.records] == ["incomplete", "incomplete"]
    assert excinfo.value.run_id == llm.recorder.runs[-1][0]


async def test_a_budget_already_at_the_ceiling_does_not_double(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_incomplete_max_output_tokens")])

    with pytest.raises(ChunkTooLargeError):
        await llm.structured(spec(max_output_tokens=16_000))

    assert len(llm.transport.calls) == 1


async def test_content_filter_raises_without_looping(gateway: Callable[..., LLMGateway]) -> None:
    """The caller marks the messages extracted; retrying a refusal just pays for it twice."""
    llm = gateway([fixture("extract_incomplete_content_filter")])

    with pytest.raises(ContentFilteredError):
        await llm.structured(spec())

    assert len(llm.transport.calls) == 1
    (record,) = llm.recorder.records
    assert record.status == "filtered"
    assert record.incomplete_reason == "content_filter"


async def test_an_unexplained_incomplete_is_a_hard_failure(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_incomplete_max_output_tokens", incomplete_details=None)])

    with pytest.raises(IncompleteResponseError) as excinfo:
        await llm.structured(spec())

    assert excinfo.value.reason is None
    assert llm.recorder.records[0].status == "incomplete"


async def test_truncated_json_is_never_parsed_leniently(
    gateway: Callable[..., LLMGateway],
) -> None:
    """A half-written events array must fail, not become a half-empty care history."""
    broken = fixture("extract_completed")
    broken["output"][0]["content"][0]["text"] = '{"events": [{"kind": "sympt'
    llm = gateway([broken])

    with pytest.raises(ResponseParseError):
        await llm.structured(spec())

    (record,) = llm.recorder.records
    assert record.status == "error"
    assert record.cost_usd > Decimal("0")  # the tokens were still burned


# --- accounting --------------------------------------------------------------------------


async def test_usage_and_cost_are_recorded_exactly(gateway: Callable[..., LLMGateway]) -> None:
    llm = gateway([fixture("extract_completed")])
    result = await llm.structured(spec())

    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (5600, 760, 250)
    assert result.cached_input_tokens == 0
    assert result.cost_usd == Decimal("0.0508")  # the per-chunk figure the cost model rests on
    assert llm.recorder.records[0].cost_usd == Decimal("0.0508")
    assert result.run_id == llm.recorder.runs[0][0]


async def test_the_fingerprint_identifies_the_request_without_storing_it(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_completed"), fixture("extract_no_events")])
    await llm.structured(spec())
    await llm.structured(spec(input="<transcript>different</transcript>"))

    first, second = llm.recorder.records
    assert len(first.request_fingerprint) == 32
    assert first.request_fingerprint != second.request_fingerprint
    assert "transcript" not in first.request_fingerprint


# --- budget ------------------------------------------------------------------------------


class _DeniedBudget:
    async def check(self, household_id: UUID | None) -> None:
        raise BudgetExceededError(f"household {household_id} is over its monthly allowance")


async def test_budget_denial_raises_and_leaves_an_audit_row(
    gateway: Callable[..., LLMGateway],
) -> None:
    llm = gateway([fixture("extract_completed")], budget=_DeniedBudget())

    with pytest.raises(BudgetExceededError) as excinfo:
        await llm.check_budget(HOUSEHOLD)

    assert llm.transport.calls == []
    (record,) = llm.recorder.records
    assert record.status == "budget_denied"
    assert record.attempts == 0
    assert record.cost_usd == Decimal("0")
    assert excinfo.value.run_id == llm.recorder.runs[0][0]


async def test_the_budget_is_checked_per_run_not_per_call(
    gateway: Callable[..., LLMGateway],
) -> None:
    """A per-call check costs a query per chunk and still cannot stop the call in flight."""
    llm = gateway([fixture("extract_completed")], budget=_DeniedBudget())
    result = await llm.structured(spec())
    assert result.parsed is not None


async def test_run_ids_are_unique_per_outcome(gateway: Callable[..., LLMGateway]) -> None:
    llm = gateway([fixture("extract_completed"), fixture("extract_no_events")])
    first = await llm.structured(spec())
    second = await llm.structured(spec())
    assert first.run_id != second.run_id
    assert {r for r, _ in llm.recorder.runs} == {first.run_id, second.run_id}


async def test_the_in_memory_recorder_sums_spend(gateway: Callable[..., LLMGateway]) -> None:
    """M1 has no database; this stand-in is what makes a backfill's spend visible anyway."""
    llm = gateway([fixture("extract_completed"), fixture("extract_completed")])
    await llm.structured(spec())
    await llm.structured(spec())
    assert llm.recorder.total_cost_usd == Decimal("0.1016")
