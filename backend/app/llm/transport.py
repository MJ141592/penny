"""The no-network seam: one Protocol with one method.

Everything above this line (retries, incomplete handling, cost accounting) is ours and is
tested offline; everything below it is the SDK. `FakeTransport` returns real
`openai.types.responses.Response` objects rehydrated from recorded JSON, so the tests
exercise our handling of the SDK's parsed shape rather than HTTP bytes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from openai import AsyncOpenAI
from openai.types.responses import Response

from app.openai_client import get_openai_client


class LLMTransport(Protocol):
    async def create(self, **kwargs: Any) -> Response: ...


class OpenAITransport:
    """The real thing. Deliberately does nothing but forward — no defaults, no fixing up."""

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> Response:
        return await self._client.responses.create(**kwargs)


@lru_cache
def get_transport() -> LLMTransport:
    """Wrap the shared client, which already sets max_retries=0 and a 90s/10s timeout.

    Reused rather than rebuilt so API-key handling lives in exactly one place; `max_retries=0`
    matters here because the gateway owns retries and every attempt must reach the audit
    trail — the SDK's silent internal retries would make attempt accounting a lie.
    """
    return OpenAITransport(get_openai_client())


class FakeTransport:
    """Replays queued fixtures in order. An `Exception` in the queue is raised instead."""

    def __init__(self, fixtures: list[Any]) -> None:
        self._queue = list(fixtures)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError(f"FakeTransport ran out of fixtures after {len(self.calls)} calls")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response.model_validate(item)
