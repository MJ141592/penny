from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


class OpenAINotConfiguredError(RuntimeError):
    """Raised when the OpenAI client is used without OPENAI_API_KEY set."""


@lru_cache
def get_openai_client() -> AsyncOpenAI:
    """Return the shared OpenAI client, built from environment settings.

    Cached so the underlying HTTP connection pool is reused across requests.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise OpenAINotConfiguredError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
