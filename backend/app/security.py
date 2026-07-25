"""Password hashing and the signed session cookie. No JWTs, no server-side session store.

The whole auth model is: one shared credential per household, and a cookie carrying nothing but
`household_id`, signed with `SESSION_SECRET`. Every tenancy decision in the app is downstream of
`read_session_cookie` returning that UUID, so the two things that matter here are that the
signature is checked before the value is trusted, and that the cookie actually reaches the
browser.

THE COOKIE FLAG THAT COSTS AN AFTERNOON: `Secure` on `http://localhost` makes the browser drop
the cookie *silently* — login returns 204, the Set-Cookie header is right there in devtools, and
the next request is anonymous. It looks exactly like a session bug. So `Secure` is set only when
`settings.env == "production"`, where the origin is HTTPS anyway.

`SameSite=Lax` (not Strict): the SPA is same-origin with the API, and Strict would drop the
cookie on a top-level navigation into the app from an external link — which is how a family
member opens it from WhatsApp.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING
from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash

from app.config import get_settings

if TYPE_CHECKING:
    from starlette.responses import Response

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "penny_session"
# Namespaces the signature: a token minted for some other purpose with the same secret cannot
# be replayed as a session.
_SESSION_SALT = "penny-session-v1"
# Used only when SESSION_SECRET is unset outside production. Cookies signed with it survive a
# reload (so dev logins persist) and are worthless to an attacker who can read this file —
# which is the point of refusing to start without a real secret in production.
_DEV_FALLBACK_SECRET = "penny-dev-insecure-session-secret"


@lru_cache
def _password_hasher() -> PasswordHash:
    # `recommended()` is argon2id with pwdlib's tuned parameters. Built once: constructing it
    # per call is not the expensive part, but the hash itself is ~50ms and belongs on one path.
    return PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-ish time by construction — argon2 does the same work for a wrong password.

    Returns False rather than raising on a malformed hash, so a corrupt row is a failed login
    and not a 500 that tells the caller the row exists.
    """
    try:
        return _password_hasher().verify(password, password_hash)
    except Exception:  # pragma: no cover - only reachable with a corrupted password_hash
        logger.warning("security.password_hash_unreadable")
        return False


@lru_cache
def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    secret = settings.session_secret
    if not secret:
        if settings.env == "production":
            raise RuntimeError(
                "SESSION_SECRET is not set. Refusing to sign session cookies in production."
            )
        logger.warning("security.session_secret_missing_using_dev_fallback")
        secret = _DEV_FALLBACK_SECRET
    return URLSafeTimedSerializer(secret, salt=_SESSION_SALT)


def _max_age_seconds() -> int:
    return get_settings().session_max_age_days * 24 * 60 * 60


def set_session_cookie(response: Response, household_id: UUID) -> None:
    """Sign `household_id` into `penny_session` on this response."""
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _serializer().dumps(str(household_id)),
        max_age=_max_age_seconds(),
        httponly=True,
        samesite="lax",
        # See the module docstring: Secure on http://localhost silently drops the cookie.
        secure=settings.env == "production",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the cookie. The attributes must match the ones it was set with.

    A `delete_cookie` with a different Path or SameSite writes a *second* cookie instead of
    replacing the first, and the browser keeps sending the original — logout appears to work
    and the next page load is still signed in.
    """
    settings = get_settings()
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.env == "production",
    )


def read_session_cookie(token: str | None) -> UUID | None:
    """The signed cookie value -> household id, or None.

    None for every failure mode there is — absent, tampered, signed with a rotated secret,
    older than SESSION_MAX_AGE_DAYS, or not a UUID. The caller cannot accidentally distinguish
    "expired" from "forged", because it should treat both identically: send them to /login.
    """
    if not token:
        return None
    try:
        raw = _serializer().loads(token, max_age=_max_age_seconds())
    except SignatureExpired:
        logger.info("security.session_expired")
        return None
    except BadSignature:
        # Worth a line: a burst of these is either a secret rotation or someone poking.
        logger.warning("security.session_bad_signature")
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        logger.warning("security.session_payload_not_a_uuid")
        return None
