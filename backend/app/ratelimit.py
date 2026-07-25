"""Who a request came from, and how many login attempts that identity has spent.

Two things live here, and they are together because getting either one wrong silently disables
the only defence a single shared family password has.

**`client_ip` is the one place the app decides who a caller is.** Every rate-limit key in the
app must come from it. Not from `request.headers["x-forwarded-for"]` at a call site, not from
`request.client` directly. One function, one review.

**`login_limiter` counts in Postgres, not in this process.** The container runs
`gunicorn --workers 2`, so a per-process dict enforces 2x the limit it claims, and any deploy
resets it. That was written down as a known boundary and it is now closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.schema import CreateTable

from app.config import get_settings
from app.db import get_engine

if TYPE_CHECKING:  # `client_ip` is called directly, never resolved by FastAPI as a dependency,
    from sqlalchemy.ext.asyncio import AsyncConnection  # so these annotations may stay strings.
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# Ten guesses per quarter hour against one shared password. Both numbers are quoted to the
# family in the 429 body, so changing either changes what the app promises.
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 15 * 60

# Only bounds the in-process FALLBACK below. The Postgres table is bounded by its own GC.
_MAX_TRACKED_KEYS = 4096

# Salts the IP hash when SESSION_SECRET is unset (dev, tests). Worthless to an attacker who can
# read this file, which is fine: off production there is nothing to correlate.
_DEV_HASH_SALT = "penny-dev-insecure-ratelimit-salt"


# --------------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    """The caller's address, as this app is willing to believe it. **Read this before editing.**

    In production Railway terminates TLS and proxies, and it sets **`X-Real-IP`** to the client
    address. That is the header to key on, and this function deliberately never looks at
    `X-Forwarded-For`:

    * The **leftmost** `X-Forwarded-For` is whatever the caller typed. Trusting it gives an
      attacker unlimited attempts (rotate it per request) *and* a lockout weapon (pin it to a
      real family's address). Both, from the same header, for free.
    * The **rightmost** entry is the proxy, not the client, so it buys nothing X-Real-IP does
      not already give us.

    Off production there is no proxy, so we use the socket peer and ignore headers entirely —
    otherwise the limiter would be one `curl -H` away from being untestable.

    Two consequences worth stating rather than discovering:

    * If Railway ever stops sending `X-Real-IP`, we fall back to the socket peer, which in
      production is the *proxy's* address and therefore shared by every caller. That fails
      closed — everyone shares one bucket and a spray locks out logins — which is the safer of
      the two failures for a health record, but it is a failure. The warning below is logged
      once per process so it is findable.
    * `request.client` is only the true peer while uvicorn's proxy-header trust stays at its
      default (loopback only). Setting `FORWARDED_ALLOW_IPS=*` makes uvicorn rewrite
      `scope["client"]` from the leftmost `X-Forwarded-For` — reintroducing exactly the bug
      this function exists to prevent, one environment variable away and invisible here. Do not
      set it.
    """
    if get_settings().env == "production":
        real_ip = _parse_ip(request.headers.get("x-real-ip"))
        if real_ip is not None:
            return real_ip
        _warn_missing_real_ip()
    return _peer_ip(request)


def _parse_ip(value: str | None) -> str | None:
    """A normalised address, or None for anything that is not one.

    Parsing rather than trusting: it caps the length, collapses the many spellings of one IPv6
    address into a single bucket, and drops junk before it becomes a hash key.
    """
    if not value:
        return None
    candidate = value.strip()[:64].strip("[]")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _peer_ip(request: Request) -> str:
    """The TCP peer. `"unknown"` (one shared bucket) when there is no client on the scope."""
    host = request.client.host if request.client else None
    return _parse_ip(host) or (host or "unknown")[:64]


_warned_missing_real_ip = False


def _warn_missing_real_ip() -> None:
    global _warned_missing_real_ip
    if not _warned_missing_real_ip:
        _warned_missing_real_ip = True
        logger.warning("ratelimit.missing_x_real_ip")


def _key_for(ip: str) -> bytes:
    """A keyed hash of the address, because an IP in a health app is personal data.

    The table stores this and never the address itself. Keyed (not a bare digest) so the row is
    not a lookup into the whole 32-bit IPv4 space; keyed with `SESSION_SECRET` so rotating that
    secret also empties the limiter, which is the correct direction to fail.
    """
    secret = get_settings().session_secret or _DEV_HASH_SALT
    return hashlib.blake2b(ip.encode(), key=secret.encode()[:64], digest_size=16).digest()


# --------------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------------

# NOT in app.models and NOT in Base.metadata, on purpose:
#
#   * It carries no household data and no history — it is a counter that may be truncated at
#     any moment with no consequence beyond a few free guesses. UNLOGGED says exactly that to
#     Postgres: no WAL, and emptied by a crash restart.
#   * Putting it in Base.metadata would put it in `alembic revision --autogenerate` output,
#     and this batch of work has exactly one migration author. `migrations/env.py`'s
#     `include_object` already ignores reflected tables that are absent from the metadata (it
#     exists for GOWA's whatsmeow tables), so autogenerate will not try to drop this one.
#
# It is created on first use with CREATE TABLE IF NOT EXISTS. Folding it into a migration later
# is a strict improvement and needs no data move; see the note at the end of this module.
_METADATA = sa.MetaData()

LOGIN_RATE_LIMITS = sa.Table(
    "login_rate_limits",
    _METADATA,
    # No IP in here. See `_key_for`.
    sa.Column("ip_hash", sa.LargeBinary, primary_key=True),
    # Start of the fixed window this row counts, floored to a multiple of the window length so
    # every worker computes the same boundary from the database's clock.
    sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("attempts", sa.Integer, nullable=False),
    prefixes=["UNLOGGED"],
)


class _InProcessWindow:
    """Fallback only: the old per-process sliding window, used when Postgres is unreachable.

    A database that is down means logins cannot succeed anyway, so this exists to keep a DB
    blip from turning into an *unlimited* guessing window, not to be the limiter.
    """

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[bytes, deque[float]] = {}

    def _prune(self, key: bytes, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def hit(self, key: bytes) -> int | None:
        now = time.monotonic()
        hits = self._prune(key, now)
        if len(hits) >= self._max:
            return max(1, int(hits[0] + self._window - now) + 1)
        if len(self._hits) >= _MAX_TRACKED_KEYS and key not in self._hits:
            # Drop whatever is coldest rather than growing without bound.
            self._hits.pop(next(iter(self._hits)), None)
        hits.append(now)
        return None

    def reset(self, key: bytes) -> None:
        self._hits.pop(key, None)


class LoginRateLimiter:
    """A fixed-window counter per (ip_hash, window), shared by every worker via Postgres.

    **One round trip per login attempt, on its own connection.** Not the request's session, and
    this is the load-bearing detail: `get_session` owns the request transaction and *rolls it
    back when the handler raises*, and a rate-limited endpoint's whole job is to count the
    attempts that raise. Recording through the request session would roll every failed attempt
    back and the limiter would count nothing but successes. So `hit` opens its own connection
    and commits — which is not a handler calling `session.commit()`, and is not implementation
    rule #1 being bent.

    **Fixed window, not sliding.** One row per address per window, one statement to increment it
    and learn the answer, and `Retry-After` is exactly "when this window ends" — a number the
    family can be told honestly. The price is the usual fixed-window edge: up to `2 * max`
    guesses spanning a boundary. Against 10 per 15 minutes that is 20 in a moment in the worst
    case, versus the unbounded budget a rotated header used to buy, and it keeps the cost at one
    statement on a path that runs for every login.

    Redis would be the textbook answer and is not a dependency here. Adding a second datastore —
    another managed service, another URL, another way to fail to boot — to count to ten a few
    times a day is not the trade this app should make. Postgres is already required for the
    request to do anything at all.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._fallback = _InProcessWindow(max_attempts, window_seconds)
        self._table_ready = False
        self._table_lock = asyncio.Lock()
        self._last_gc = 0.0

    async def hit(self, ip: str) -> int | None:
        """Record one attempt. Returns seconds to wait if the caller is *over* the limit.

        Over-limit attempts are counted no further, so hammering the endpoint does not extend
        anyone's lockout: the window ends when it was always going to end.
        """
        key = _key_for(ip)
        try:
            async with get_engine().begin() as conn:
                await self._ensure_table()
                attempts, retry_after = (await conn.execute(self._upsert(key))).one()
                await self._maybe_collect(conn)
        except Exception as exc:
            # The type, not the traceback: an unreachable Postgres means one of these per login
            # attempt, and a stack trace per attempt buries the outage in its own noise.
            logger.warning("ratelimit.store_unavailable", extra={"exc_type": type(exc).__name__})
            return self._fallback.hit(key)
        return int(retry_after) if attempts > self._max else None

    async def reset(self, ip: str) -> None:
        """Clear a caller's window after a successful login.

        A family that fat-fingers the password three times and then gets in is not three
        attempts away from being locked out of their own health record for a quarter of an hour.
        """
        key = _key_for(ip)
        self._fallback.reset(key)
        try:
            async with get_engine().begin() as conn:
                await conn.execute(
                    sa.delete(LOGIN_RATE_LIMITS).where(LOGIN_RATE_LIMITS.c.ip_hash == key)
                )
        except Exception as exc:
            # A failed reset costs a family some retries, never a lockout that outlives the
            # window. Not worth failing a login that has already succeeded.
            logger.warning("ratelimit.reset_failed", extra={"exc_type": type(exc).__name__})

    def _upsert(self, key: bytes) -> sa.Executable:
        """INSERT-or-increment, and tell me the count and when the window ends. One statement.

        `least(attempts + 1, max + 1)` caps the counter one past the limit: enough to know the
        caller is over, bounded so a sustained attack cannot overflow an int, and — because the
        row is never touched again — the returned `Retry-After` stays fixed instead of sliding
        forward every time the attacker retries.
        """
        window_start = sa.func.to_timestamp(
            sa.func.floor(sa.extract("epoch", sa.func.now()) / self._window) * self._window
        )
        # All epoch arithmetic, no INTERVAL literal: asyncpg cannot infer the type of a bound
        # timedelta here and fails with "could not determine data type of parameter".
        retry_after = sa.func.greatest(
            1,
            sa.cast(
                sa.func.ceil(
                    sa.extract("epoch", LOGIN_RATE_LIMITS.c.window_start)
                    + self._window
                    - sa.extract("epoch", sa.func.now())
                ),
                sa.Integer,
            ),
        )
        return (
            pg_insert(LOGIN_RATE_LIMITS)
            .values(ip_hash=key, window_start=window_start, attempts=1)
            .on_conflict_do_update(
                index_elements=[LOGIN_RATE_LIMITS.c.ip_hash, LOGIN_RATE_LIMITS.c.window_start],
                set_={"attempts": sa.func.least(LOGIN_RATE_LIMITS.c.attempts + 1, self._max + 1)},
            )
            .returning(LOGIN_RATE_LIMITS.c.attempts, retry_after.label("retry_after"))
        )

    async def _ensure_table(self) -> None:
        """CREATE TABLE IF NOT EXISTS, once per process, in its own transaction.

        Its own transaction because a DDL error must not abort the upsert's, and because two
        workers racing on IF NOT EXISTS is a documented Postgres race that the clause does not
        actually close — the loser gets a duplicate-key error from the catalog. Losing that race
        means the table exists, which is all we wanted.
        """
        if self._table_ready:
            return
        async with self._table_lock:
            if self._table_ready:
                return
            try:
                async with get_engine().begin() as conn:
                    await conn.execute(CreateTable(LOGIN_RATE_LIMITS, if_not_exists=True))
            except Exception:
                async with get_engine().connect() as conn:
                    exists = await conn.run_sync(
                        lambda sync_conn: sa.inspect(sync_conn).has_table(LOGIN_RATE_LIMITS.name)
                    )
                if not exists:
                    raise
            self._table_ready = True

    async def _maybe_collect(self, conn: AsyncConnection) -> None:
        """Drop windows that have closed, at most once per window per process.

        Piggybacks on the connection and transaction the attempt already opened, so it costs no
        extra round trip. Rows are ~30 bytes and only real addresses can create one now, but a
        table that only grows is a table that eventually needs an operator.
        """
        now = time.monotonic()
        if now - self._last_gc < self._window:
            return
        self._last_gc = now
        cutoff = sa.func.to_timestamp(sa.extract("epoch", sa.func.now()) - 2 * self._window)
        await conn.execute(
            sa.delete(LOGIN_RATE_LIMITS).where(LOGIN_RATE_LIMITS.c.window_start < cutoff)
        )


login_limiter = LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)

# WHEN THIS TABLE JOINS ALEMBIC: add it to app.models as an ordinary model and delete
# `_ensure_table` plus its call. Nothing migrates — an empty counter table is the correct
# starting state, and every row in it expires within two windows anyway.
