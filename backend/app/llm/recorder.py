"""The `RunRecorder` that writes `llm_runs`. One row per ATTEMPT, failures included.

`app.llm.gateway` deliberately owns no SQLAlchemy import: it records through the `RunRecorder`
Protocol so M1 could run the whole extraction pipeline with no database at all. This module is
the other half — drop it into `LLMGateway(recorder=...)` and every call the gateway makes lands
in `llm_runs`, including the ones that raised.

THREE THINGS THIS FILE IS CAREFUL ABOUT

1. **A failure is the row that matters most.** The gateway calls `record()` before it re-raises
   on a transport error, a truncated response and a content filter. If this recorder only
   handled the happy path, the audit trail would say a household spent nothing on the days it
   spent the most — truncated responses bill for every token they burned.

2. **One `AsyncSession` is not concurrency-safe.** The runner extracts four chunks with
   `asyncio.gather`, so four `record()` calls can be in flight at once on the same session, and
   asyncpg answers concurrent work on one connection with `InterfaceError: another operation is
   in progress`. The lock below is what makes the recorder safe to share.

3. **It never commits.** `app.db.get_session` owns the transaction boundary (implementation
   rule #1). `flush()` is enough to get the generated id the gateway returns to its caller.

NEVER LOG PROMPT TEXT. `request_fingerprint` is a hash for exactly that reason; it is safe to
store and safe to log, and the text it came from is neither.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa

from app.models import LlmRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.gateway import LLMRunRecord

# `llm_runs.error` is a Text column an operator reads in a query result, not a stack trace
# store. Long enough for "ChunkTooLargeError: max_output_tokens", short enough to stay a line.
MAX_ERROR_CHARS = 500


class DbRunRecorder:
    """Writes one `llm_runs` row per gateway attempt, in the caller's transaction.

    `household_id` is a constructor argument AND read off each record because
    `LLMRunRecord.household_id` is optional — the CLI and the eval have no household — while
    `llm_runs.household_id` is NOT NULL like every other tenant-scoped column in the schema.
    The constructor value is the tenant this recorder belongs to; a record that names a
    different household is a bug in the caller, not a row to write, so it is rejected loudly
    rather than filed under the wrong family.
    """

    def __init__(self, session: AsyncSession, household_id: UUID) -> None:
        self._session = session
        self._household_id = household_id
        self._lock = asyncio.Lock()
        self.run_ids: list[UUID] = []

    async def record(self, run: LLMRunRecord) -> UUID:
        if run.household_id is not None and run.household_id != self._household_id:
            raise ValueError(
                "LLMRunRecord names a different household than this recorder was built for."
            )
        values = {
            "household_id": self._household_id,
            "purpose": run.purpose,
            "status": run.status,
            "model": run.model,
            "prompt_version": run.prompt_version,
            "reasoning_effort": run.reasoning_effort,
            # Empty string means "no request was made" (a budget denial). NULL says that
            # better than "" does, and the column is nullable for exactly this case.
            "request_fingerprint": run.request_fingerprint or None,
            "response_id": run.response_id,
            "attempts": run.attempts,
            "input_tokens": run.input_tokens,
            "cached_input_tokens": run.cached_input_tokens,
            "output_tokens": run.output_tokens,
            "reasoning_tokens": run.reasoning_tokens,
            "cost_usd": run.cost_usd,
            "latency_ms": run.latency_ms,
            "error": _error_text(run),
            "finished_at": datetime.now(UTC),
        }
        async with self._lock:
            result = await self._session.execute(
                sa.insert(LlmRun).values(**values).returning(LlmRun.id)
            )
            run_id = result.scalar_one()
        self.run_ids.append(run_id)
        return run_id


def _error_text(run: LLMRunRecord) -> str | None:
    """`error_class` and `incomplete_reason` are two different failures, so keep both.

    `incomplete: max_output_tokens` and `error: BadRequestError` need different fixes, and an
    operator reading one column should not have to guess which one they are looking at.
    """
    parts = [part for part in (run.error_class, run.incomplete_reason) if part]
    if not parts:
        return None
    return ": ".join(parts)[:MAX_ERROR_CHARS]
