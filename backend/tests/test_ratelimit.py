"""The two things about login throttling that fail silently when they regress.

Neither is visible from a passing login. A limiter keyed on the wrong header still returns 429
in a test that sends one request ten times, and a per-process limiter still returns 429 when
the test only ever talks to one process. So these assert the properties, not the status code:

1. `X-Forwarded-For` never reaches the key. It is caller-controlled, and trusting it is both a
   bypass (rotate it) and a lockout weapon (pin it to a family's address).
2. Two workers share one counter, so the limit is the number it says and not twice it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
import sqlalchemy as sa
from starlette.requests import Request

from app.config import Settings
from app.db import dispose_engine, get_engine
from app.ratelimit import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_RATE_LIMITS,
    LOGIN_WINDOW_SECONDS,
    LoginRateLimiter,
    client_ip,
)

VICTIM_IP = "203.0.113.5"
PROXY_IP = "10.0.0.1"


def make_request(peer: str = PROXY_IP, **headers: str) -> Request:
    """A real Starlette request, so the header lookup under test is the real one."""
    return Request(
        {
            "type": "http",
            "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 44321),
        }
    )


def test_production_keys_on_x_real_ip_and_ignores_forwarded_for(
    settings_override: Callable[..., Settings],
) -> None:
    """The regression this file exists for. Railway sets X-Real-IP; the caller sets XFF."""
    settings_override(env="production")

    rotated = [
        client_ip(make_request(x_real_ip=VICTIM_IP, x_forwarded_for=f"198.51.100.{n}, {PROXY_IP}"))
        for n in range(5)
    ]

    # One bucket, not five: rotating the header buys no extra attempts.
    assert rotated == [VICTIM_IP] * 5


def test_production_falls_back_to_the_peer_never_to_forwarded_for(
    settings_override: Callable[..., Settings],
) -> None:
    """No X-Real-IP is a proxy misconfiguration, and it must fail closed, not open.

    Keying on the XFF value here would hand an attacker exactly the bypass and the lockout that
    trusting it always did — the missing header is the tempting place to reintroduce it.
    """
    settings_override(env="production")

    key = client_ip(make_request(peer=PROXY_IP, x_forwarded_for=f"192.0.2.77, {PROXY_IP}"))

    assert key == PROXY_IP


def test_off_production_uses_the_peer_and_reads_no_headers(
    settings_override: Callable[..., Settings],
) -> None:
    """There is no proxy in dev, so a header is only ever a way to spoof the limiter."""
    settings_override(env="dev")

    key = client_ip(make_request(peer="127.0.0.1", x_real_ip=VICTIM_IP, x_forwarded_for=VICTIM_IP))

    assert key == "127.0.0.1"


@pytest.fixture
async def shared_store(
    db_url: str, settings_override: Callable[..., Settings]
) -> AsyncIterator[None]:
    """Point the limiter at real Postgres under a session secret unique to this run.

    The secret keys the IP hash, so a fresh one gives this test its own key space and no
    cleanup race with a concurrently running app or another test.
    """
    settings_override(env="test", database_url=db_url, session_secret=uuid4().hex)
    await dispose_engine()
    try:
        yield
    finally:
        async with get_engine().begin() as conn:
            await conn.execute(sa.delete(LOGIN_RATE_LIMITS))
        await dispose_engine()


async def test_two_workers_share_one_counter(shared_store: None) -> None:
    """Two limiter instances are two gunicorn workers. The budget must not be per-process.

    Attempts alternate between them, so a per-process counter would need 2 * LOGIN_MAX_ATTEMPTS
    before either said no — the exact bug this replaced.
    """
    worker_a = LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)
    worker_b = LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)

    results = [
        await (worker_a if n % 2 == 0 else worker_b).hit(VICTIM_IP)
        for n in range(LOGIN_MAX_ATTEMPTS + 1)
    ]

    assert results[:LOGIN_MAX_ATTEMPTS] == [None] * LOGIN_MAX_ATTEMPTS
    retry_after = results[LOGIN_MAX_ATTEMPTS]
    # Honest: a number of seconds inside the window we actually enforce, so Retry-After and the
    # sentence in the body can both be believed.
    assert retry_after is not None and 0 < retry_after <= LOGIN_WINDOW_SECONDS

    # A successful login on either worker clears it for both.
    await worker_a.reset(VICTIM_IP)
    assert await worker_b.hit(VICTIM_IP) is None


async def test_the_stored_row_carries_no_address(shared_store: None) -> None:
    """An IP is personal data and this is a health app: the table stores a keyed hash."""
    await LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS).hit(VICTIM_IP)

    async with get_engine().begin() as conn:
        stored = (await conn.execute(sa.select(LOGIN_RATE_LIMITS.c.ip_hash))).scalars().all()

    assert stored, "the attempt should have been recorded"
    assert all(VICTIM_IP.encode() not in row for row in stored)
