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
async def test_chat_completion_round_trip() -> None:
    settings = get_settings()
    response = await get_openai_client().chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        max_tokens=5,
    )
    assert response.choices[0].message.content.strip().lower() == "pong"
    assert response.usage.completion_tokens > 0
