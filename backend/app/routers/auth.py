"""Login, logout, and the session probe the SPA boots on.

Two properties this file exists to guarantee.

**A wrong username and a wrong password are indistinguishable.** Same status, same body, and —
the part that is easy to skip — the *same work done*. When the username misses, we still run a
full argon2 verification against a fixed dummy hash, because argon2 takes ~50ms and an endpoint
that returns in 2ms for "no such household" and 55ms for "wrong password" is a user-enumeration
oracle measurable over the internet. There is one shared family credential; the username is half
of it.

**Login is rate limited.** One shared password is a single guessable secret with no lockout
story and no second factor, so the only thing standing between a dictionary and a family's
health record is how many guesses per minute an attacker gets.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from functools import lru_cache

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import CurrentHousehold, SessionDep
from app.errors import UnauthorizedError
from app.models import Event, Household, Message
from app.schemas import SessionCounts, SessionOut, to_household
from app.security import (
    clear_session_cookie,
    hash_password,
    set_session_cookie,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])

# Identical for a bad username and a bad password. Never say which.
INVALID_CREDENTIALS_DETAIL = "Invalid username or password."
RATE_LIMITED_DETAIL = "Too many attempts. Try again in a minute."


def _rate_limited_detail(retry_after: int) -> str:
    """The contract's sentence, but with the real wait in it.

    A family locked out for a quarter of an hour and told "try again in a minute" retries,
    fails, and concludes the app is broken. The shape is the contract's; only the duration is
    computed, and `Retry-After` carries the exact number of seconds regardless.
    """
    if retry_after <= 90:
        return RATE_LIMITED_DETAIL
    minutes = max(1, round(retry_after / 60))
    return f"Too many attempts. Try again in about {minutes} minutes."


RATE_LIMIT_MAX_ATTEMPTS = 10
RATE_LIMIT_WINDOW_SECONDS = 15 * 60
# Bounds the memory a spray across forged X-Forwarded-For values can cost us.
RATE_LIMIT_MAX_TRACKED_IPS = 4096


class LoginRequest(BaseModel):
    # Bounded, because both values go into an argon2 call: an unbounded password is a free
    # CPU-burn endpoint.
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


# `HouseholdOut` / `SessionCounts` / `SessionOut` are deliberately NOT redefined here — they
# come from `app.schemas`, which is also what `PATCH /api/household` returns. Two same-named
# models in one app is not a style question: FastAPI keys OpenAPI components by class name, so
# the moment these two definitions diverge by one field, `/openapi.json` grows a mangled pair
# and `openapi-typescript` regenerates `frontend/src/api/types.ts` with two divergent
# `Household` types. They were field-identical when this was consolidated, so the wire format
# is unchanged — this only removes the way it could silently stop being.


@lru_cache
def _dummy_password_hash() -> str:
    """A real argon2 hash of a value nothing can match, for the username-miss path.

    Computed lazily and once. It must be a genuine hash from the same hasher, so verifying
    against it costs exactly what verifying a real one costs.
    """
    return hash_password("penny-nonexistent-account-placeholder")


class _SlidingWindowLimiter:
    """Per-IP sliding window, in process memory.

    KNOWN BOUNDARY, not an oversight: **this stops working with more than one replica.** Each
    process keeps its own counters, so N replicas behind a load balancer give an attacker N
    times the budget, and a rolling deploy resets it. Penny runs as a single Railway instance
    today; the moment it does not, this has to move to Postgres or Redis. It is here rather
    than nowhere because a single shared password with no limiter at all is the worse failure,
    and it is here rather than in a library because the whole thing is fifteen lines.
    """

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def retry_after(self, key: str, now: float | None = None) -> int | None:
        """Seconds to wait if the caller is over the limit, else None. Does not record."""
        now = time.monotonic() if now is None else now
        hits = self._prune(key, now)
        if len(hits) < self._max:
            return None
        return max(1, int(hits[0] + self._window - now) + 1)

    def record(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._hits) >= RATE_LIMIT_MAX_TRACKED_IPS and key not in self._hits:
            # Drop whatever is coldest rather than growing without bound. Evicting a real
            # attacker's bucket is possible but requires already sustaining thousands of
            # distinct source addresses, at which point the limiter is not the defence.
            self._hits.pop(next(iter(self._hits)), None)
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        """Called on a successful login: a family that fat-fingers twice then gets in is not
        one attempt away from being locked out for a quarter of an hour."""
        self._hits.pop(key, None)


_login_limiter = _SlidingWindowLimiter(RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS)


def _client_key(request: Request) -> str:
    """The identity the limiter counts against.

    In production Railway terminates TLS and proxies, so `request.client.host` is the router
    for every caller and counting on it would let one attacker lock out the whole world. The
    leftmost X-Forwarded-For is the client's own claim and is spoofable — the trade we accept
    is "an attacker can rotate the header to get more attempts" over "one attacker denies
    service to every family". Off production we use the peer address, because there is no
    proxy and trusting the header would make the limiter trivially bypassable in tests.
    """
    if get_settings().env == "production":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


@router.post("/auth/login", status_code=204)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> Response:
    key = _client_key(request)
    retry_after = _login_limiter.retry_after(key)
    if retry_after is not None:
        logger.warning("auth.login_rate_limited", extra={"retry_after": retry_after})
        raise HTTPException(
            status_code=429,
            detail=_rate_limited_detail(retry_after),
            headers={"Retry-After": str(retry_after)},
        )
    _login_limiter.record(key)

    household = (
        await session.execute(sa.select(Household).where(Household.username == payload.username))
    ).scalar_one_or_none()

    # Both branches run one argon2 verification. Do not "optimise" the miss path.
    password_hash = household.password_hash if household else _dummy_password_hash()
    ok = verify_password(payload.password, password_hash)

    if household is None or not ok:
        # No username in the log line either: it is half of a shared credential, and a log
        # aggregator is a worse place for it than a database.
        logger.info("auth.login_failed")
        raise UnauthorizedError(INVALID_CREDENTIALS_DETAIL)

    _login_limiter.reset(key)
    logger.info("auth.login_ok", extra={"household_id": str(household.id)})
    response = Response(status_code=204)
    set_session_cookie(response, household.id)
    return response


@router.post("/auth/logout", status_code=204)
async def logout() -> Response:
    """Deliberately takes no session dependency: signing out must work even when the cookie is
    already junk. Requiring a valid session to clear one leaves a user with a bad cookie
    unable to get rid of it."""
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=SessionOut)
async def me(ctx: CurrentHousehold, session: SessionDep) -> SessionOut:
    """The probe the SPA boots on. A 401 here is the normal signed-out state, not an error."""
    counts = (
        await session.execute(
            sa.select(
                sa.select(sa.func.count())
                .select_from(Event)
                .where(Event.household_id == ctx.id, Event.deleted_at.is_(None))
                .scalar_subquery(),
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.household_id == ctx.id)
                .scalar_subquery(),
            )
        )
    ).one()
    return SessionOut(
        # `to_household` is the one place a row becomes this object, so `password_hash` can
        # never arrive here by someone reaching for `model_validate(row, from_attributes=True)`.
        household=to_household(ctx.household),
        counts=SessionCounts(events=counts[0], messages=counts[1]),
    )
