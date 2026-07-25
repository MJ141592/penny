"""The one wrapper around the Responses API: retries, incomplete handling, cost, audit.

NEVER LOG MESSAGE TEXT, PROMPT TEXT OR MODEL OUTPUT. This module handles a family's health
conversation. Log message handles, household id, character counts and token counts — nothing
that could reconstruct what was said. Every logging call below obeys that; keep it that way.

Two more rules the shape of this module enforces rather than documents:
  * `temperature` and `top_p` are unrepresentable on `CallSpec`, therefore unsendable.
  * `max_output_tokens` has no default anywhere. One call at the 128,000 ceiling costs $3.84.

No SQLAlchemy import: the audit trail goes through the `RunRecorder` Protocol so M1 runs with
no database at all and M4 drops in a recorder that writes the `llm_runs` table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from openai.types.responses import Response
from pydantic import BaseModel, ValidationError

from app import errors
from app.config import get_settings
from app.llm.pricing import cost_usd
from app.llm.schemas import to_strict_json_schema
from app.llm.transport import LLMTransport, get_transport

log = logging.getLogger(__name__)

Purpose = Literal["extract", "merge", "report", "dedup_check"]
Effort = Literal["none", "low", "medium", "high", "xhigh"]
RunStatus = Literal["ok", "incomplete", "filtered", "error", "budget_denied"]

MAX_ATTEMPTS = 3
# The ceiling the doubled-budget retry may not exceed: 16,000 output tokens is $0.48.
MAX_OUTPUT_TOKENS_CEILING = 16_000
# Process-wide, not per household: it exists to protect our own event loop and OpenAI's
# rate limits during a backfill, and a backfill is one household at a time anyway.
_CONCURRENCY = asyncio.Semaphore(4)

_EFFORT_LADDER: tuple[Effort, ...] = ("none", "low", "medium", "high", "xhigh")


class GatewayError(Exception):
    """Base for every failure the gateway raises with an audit row already written."""

    def __init__(self, message: str, run_id: UUID | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ChunkTooLargeError(GatewayError):
    """Still truncated after a doubled budget. The runner splits the chunk and recurses."""

    def __init__(self, run_id: UUID | None = None) -> None:
        super().__init__("Response still incomplete after a doubled output budget", run_id)


class ContentFilteredError(GatewayError):
    """The model refused. The caller marks the messages extracted and does NOT loop."""

    def __init__(self, run_id: UUID | None = None) -> None:
        super().__init__("Response stopped by the content filter", run_id)


class IncompleteResponseError(GatewayError):
    def __init__(self, reason: str | None, run_id: UUID | None = None) -> None:
        super().__init__(f"Response incomplete: {reason}", run_id)
        self.reason = reason


class ResponseParseError(GatewayError):
    """The body was absent or not valid instance of the schema. Never parsed leniently."""


class BudgetExceededError(GatewayError, errors.BudgetExceededError):
    """This household has spent its monthly allowance. Checked once per run.

    BOTH BASES ON PURPOSE. `GatewayError` carries `run_id`, so the refusal still has an
    `llm_runs` row; `app.errors.BudgetExceededError` carries `status_code = 409`, so when this
    propagates out of the import route it renders as the 409 docs/api-contract.md promises
    instead of a generic 500. Two separately-defined classes of the same name is how a spend
    refusal quietly becomes "Something went wrong on our end."

    `GatewayError.__init__` stays in charge, and its `message` becomes the client-visible
    `detail` — so whatever a `BudgetGuard` raises must be a sentence safe to show a family.
    """

    def __init__(self, message: str | None = None, run_id: UUID | None = None) -> None:
        # Default to the contract's sentence, so `raise BudgetExceededError()` reads the same
        # as every other error in app.errors and cannot accidentally render an empty detail.
        super().__init__(message or errors.BudgetExceededError.detail, run_id)


@dataclass(frozen=True, slots=True)
class CallSpec[T: BaseModel]:
    purpose: Purpose
    instructions: str
    input: str
    schema: type[T]
    max_output_tokens: int  # REQUIRED. No default, at any layer.
    reasoning_effort: Effort = "low"
    household_id: UUID | None = None
    model: str | None = None
    prompt_version: str | None = None
    # temperature / top_p intentionally absent: unrepresentable, therefore unsendable.


@dataclass(slots=True)
class LLMResult[T: BaseModel]:
    parsed: T
    run_id: UUID
    response_id: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: Decimal
    attempts: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LLMRunRecord:
    """One row of the audit trail. Written for EVERY outcome, including failures — a failed
    run with zero tokens is still the record that the call happened."""

    household_id: UUID | None
    purpose: Purpose
    model: str
    request_fingerprint: str  # sha256(instructions + input)[:32]; never the text itself
    status: RunStatus
    attempts: int
    reasoning_effort: Effort
    incomplete_reason: str | None = None
    error_class: str | None = None
    response_id: str | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    prompt_version: str | None = None


class RunRecorder(Protocol):
    async def record(self, run: LLMRunRecord) -> UUID: ...


@dataclass(slots=True)
class InMemoryRunRecorder:
    """The default, so the gateway runs with no database. M4 swaps in an llm_runs writer."""

    runs: list[tuple[UUID, LLMRunRecord]] = field(default_factory=list)

    async def record(self, run: LLMRunRecord) -> UUID:
        run_id = uuid4()
        self.runs.append((run_id, run))
        return run_id

    @property
    def records(self) -> list[LLMRunRecord]:
        return [run for _, run in self.runs]

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((run.cost_usd for run in self.records), Decimal("0"))


class BudgetGuard(Protocol):
    """Checked once per RUN, not per call — a per-call check costs a query per chunk and
    still cannot stop the call already in flight."""

    async def check(self, household_id: UUID | None) -> None: ...


class NoBudgetGuard:
    async def check(self, household_id: UUID | None) -> None:
        return None


def _safety_identifier(household_id: UUID | None) -> str | None:
    """A stable pseudonym for abuse reporting that is not the tenant id itself."""
    if household_id is None:
        return None
    return hashlib.sha256(str(household_id).encode()).hexdigest()[:32]


def _fingerprint(instructions: str, input_text: str) -> str:
    return hashlib.sha256((instructions + input_text).encode()).hexdigest()[:32]


def _notch_down(effort: Effort) -> Effort:
    index = _EFFORT_LADDER.index(effort)
    return _EFFORT_LADDER[max(index - 1, 0)]


def _retry_after_seconds(error: Exception) -> float | None:
    """Honour the server's own pacing over our backoff curve when it sends one."""
    response = getattr(error, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:  # HTTP-date form; our own backoff is a fine approximation
        return None


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, BadRequestError):
        # A 400 is a schema or parameter bug. Retrying burns money and hides the defect.
        return False
    transient = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
    if isinstance(error, transient):
        return True
    return isinstance(error, APIStatusError) and error.status_code >= 500


class LLMGateway:
    def __init__(
        self,
        transport: LLMTransport | None = None,
        recorder: RunRecorder | None = None,
        budget: BudgetGuard | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport or get_transport()
        self.recorder = recorder or InMemoryRunRecorder()
        self._budget = budget or NoBudgetGuard()
        self._sleep = sleep

    async def check_budget(
        self,
        household_id: UUID | None,
        *,
        purpose: Purpose = "extract",
        model: str | None = None,
    ) -> None:
        """Call ONCE at the top of a run. Raises BudgetExceededError with an audit row."""
        try:
            await self._budget.check(household_id)
        except BudgetExceededError as error:
            run_id = await self.recorder.record(
                LLMRunRecord(
                    household_id=household_id,
                    purpose=purpose,
                    model=model or get_settings().llm_model_extract,
                    request_fingerprint="",
                    status="budget_denied",
                    attempts=0,
                    reasoning_effort="none",
                    error_class=type(error).__name__,
                )
            )
            error.run_id = run_id
            log.warning("llm budget denied household=%s purpose=%s", household_id, purpose)
            raise

    async def structured[T: BaseModel](self, spec: CallSpec[T]) -> LLMResult[T]:
        return await self._structured(spec, budget_retried=False)

    async def _structured[T: BaseModel](
        self, spec: CallSpec[T], *, budget_retried: bool
    ) -> LLMResult[T]:
        # Extraction is the default because it is the overwhelming majority of calls; the
        # report generator passes settings.llm_model_report explicitly.
        model = spec.model or get_settings().llm_model_extract
        fingerprint = _fingerprint(spec.instructions, spec.input)
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": spec.instructions,
            "input": spec.input,
            "reasoning": {"effort": spec.reasoning_effort},
            "max_output_tokens": spec.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": spec.schema.__name__,
                    "schema": to_strict_json_schema(spec.schema),
                    "strict": True,
                }
            },
            # Family health chat: we do not retain it server-side.
            "store": False,
        }
        # OMITTED, not sent as null, when there is no household — the CLI and the eval have
        # none, and an explicit null is a value the API is entitled to reject on every call.
        identifier = _safety_identifier(spec.household_id)
        if identifier is not None:
            kwargs["safety_identifier"] = identifier

        started = time.monotonic()
        attempts = 0
        async with _CONCURRENCY:
            while True:
                attempts += 1
                try:
                    raw = await self.transport.create(**kwargs)
                    break
                except Exception as error:
                    if not _is_retryable(error) or attempts >= MAX_ATTEMPTS:
                        if isinstance(error, BadRequestError):
                            # The schema is the usual culprit and it is not secret, unlike
                            # everything else in this call.
                            log.error(
                                "llm bad request purpose=%s model=%s schema=%s",
                                spec.purpose,
                                model,
                                json.dumps(kwargs["text"]["format"]["schema"]),
                            )
                        await self._record(
                            spec,
                            model,
                            fingerprint,
                            status="error",
                            attempts=attempts,
                            latency_ms=_elapsed_ms(started),
                            error_class=type(error).__name__,
                        )
                        raise
                    delay = _retry_after_seconds(error) or _backoff(attempts - 1)
                    log.warning(
                        "llm retry purpose=%s attempt=%d error=%s delay=%.2f",
                        spec.purpose,
                        attempts,
                        type(error).__name__,
                        delay,
                    )
                    await self._sleep(delay)

        latency_ms = _elapsed_ms(started)
        usage = _Usage.of(raw)
        spend = cost_usd(model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)

        if raw.status == "incomplete":
            reason = raw.incomplete_details.reason if raw.incomplete_details else None
            # Tokens burned on a truncated response are still billed, so record before raising.
            run_id = await self._record(
                spec,
                model,
                fingerprint,
                status="filtered" if reason == "content_filter" else "incomplete",
                attempts=attempts,
                latency_ms=latency_ms,
                usage=usage,
                spend=spend,
                response_id=raw.id,
                incomplete_reason=reason,
            )
            if reason == "content_filter":
                raise ContentFilteredError(run_id)
            if reason != "max_output_tokens":
                raise IncompleteResponseError(reason, run_id)
            if budget_retried or spec.max_output_tokens >= MAX_OUTPUT_TOKENS_CEILING:
                raise ChunkTooLargeError(run_id)
            # Reasoning tokens are the usual culprit and they bill at the output rate, so
            # notch effort down as well as raising the ceiling.
            return await self._structured(
                replace(
                    spec,
                    max_output_tokens=min(MAX_OUTPUT_TOKENS_CEILING, spec.max_output_tokens * 2),
                    reasoning_effort=_notch_down(spec.reasoning_effort),
                ),
                budget_retried=True,
            )

        try:
            parsed = spec.schema.model_validate_json(raw.output_text)
        except (ValidationError, ValueError) as error:
            run_id = await self._record(
                spec,
                model,
                fingerprint,
                status="error",
                attempts=attempts,
                latency_ms=latency_ms,
                usage=usage,
                spend=spend,
                response_id=raw.id,
                error_class=type(error).__name__,
            )
            raise ResponseParseError(
                f"{spec.schema.__name__} did not parse from a {raw.status} response", run_id
            ) from error

        run_id = await self._record(
            spec,
            model,
            fingerprint,
            status="ok",
            attempts=attempts,
            latency_ms=latency_ms,
            usage=usage,
            spend=spend,
            response_id=raw.id,
        )
        log.info(
            "llm ok purpose=%s household=%s in=%d cached=%d out=%d reasoning=%d cost=%s ms=%d",
            spec.purpose,
            spec.household_id,
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
            spend,
            latency_ms,
        )
        return LLMResult(
            parsed=parsed,
            run_id=run_id,
            response_id=raw.id,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=spend,
            attempts=attempts,
            latency_ms=latency_ms,
        )

    async def _record(
        self,
        spec: CallSpec[Any],
        model: str,
        fingerprint: str,
        *,
        status: RunStatus,
        attempts: int,
        latency_ms: int,
        usage: _Usage | None = None,
        spend: Decimal = Decimal("0"),
        response_id: str | None = None,
        incomplete_reason: str | None = None,
        error_class: str | None = None,
    ) -> UUID:
        usage = usage or _Usage(0, 0, 0, 0)
        return await self.recorder.record(
            LLMRunRecord(
                household_id=spec.household_id,
                purpose=spec.purpose,
                model=model,
                request_fingerprint=fingerprint,
                status=status,
                attempts=attempts,
                reasoning_effort=spec.reasoning_effort,
                incomplete_reason=incomplete_reason,
                error_class=error_class,
                response_id=response_id,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cost_usd=spend,
                latency_ms=latency_ms,
                prompt_version=spec.prompt_version,
            )
        )


@dataclass(frozen=True, slots=True)
class _Usage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int

    @classmethod
    def of(cls, raw: Response) -> _Usage:
        if raw.usage is None:
            return cls(0, 0, 0, 0)
        return cls(
            input_tokens=raw.usage.input_tokens,
            cached_input_tokens=raw.usage.input_tokens_details.cached_tokens,
            output_tokens=raw.usage.output_tokens,
            reasoning_tokens=raw.usage.output_tokens_details.reasoning_tokens,
        )


def _backoff(attempt: int) -> float:
    return min(8.0, 0.75 * 2**attempt) + random.uniform(0, 0.4)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
