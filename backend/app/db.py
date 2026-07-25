"""Async engine, session factory, and the one place a transaction begins and ends."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def to_asyncpg_url(url: str) -> str:
    """Railway (and .env.example) hand out `postgresql://`; asyncpg needs it in the scheme.

    `Settings.database_url` already normalises itself, but `PENNY_TEST_DATABASE_URL` and any URL
    pasted on a command line do not, and the failure is an unhelpful "can't load plugin".
    """
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+asyncpg://" + url[len(scheme) :]
    return url


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set; the app cannot reach Postgres.")
    return create_async_engine(
        settings.database_url,
        # Railway closes idle connections; without pre-ping the first request after a quiet
        # spell fails with a stale connection instead of transparently reconnecting.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: get_session commits at the END of the request, and an expired
    # instance would then re-fetch (on a closed session) while FastAPI serialises the response.
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that OWNS THE TRANSACTION BOUNDARY.

    It commits when the handler returns and rolls back when it raises. **Handlers therefore
    never call `session.commit()` themselves** — implementation rule #1. A handler that commits
    mid-request splits one logical write into two, so a later failure leaves half of it durable:
    an import row with no messages, an event with no llm_run. Use `await session.flush()` when
    you need a generated id inside the handler.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine() -> None:
    """Close the pool — app shutdown, and tests that swap the URL between cases."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
