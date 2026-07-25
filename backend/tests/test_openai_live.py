"""Live smoke test — hits the real OpenAI API and costs money.

Skipped by default. Run with:

    RUN_LIVE_OPENAI_TESTS=1 uv run pytest -m live
"""

import os

import pytest

from app.config import get_settings
from app.openai_client import get_openai_client

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_OPENAI_TESTS") != "1",
        reason="live API test; set RUN_LIVE_OPENAI_TESTS=1 to run",
    ),
]


@pytest.mark.asyncio
async def test_responses_round_trip() -> None:
    """Proves the model id, the Responses API shape and the key work together.

    gpt-5.5 rejects max_tokens outright, and reasoning tokens bill against
    max_output_tokens, so 64 with effort="none" is the cheapest honest round trip.
    """
    settings = get_settings()
    response = await get_openai_client().responses.create(
        model=settings.llm_model_extract,
        input="Reply with exactly: pong",
        max_output_tokens=64,
        reasoning={"effort": "none"},
    )
    # A truncated response still carries text; without this it would pass silently.
    assert response.status == "completed"
    assert response.output_text.strip().lower() == "pong"
    assert response.usage.output_tokens > 0
