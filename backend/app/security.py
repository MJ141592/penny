"""Password hashing and the signed session cookie. No JWTs, no server-side session store.

The whole auth model is: one shared credential per household, and a cookie carrying
`household_id` and the household's `session_version`, signed with `SESSION_SECRET`. Every
tenancy decision in the app is downstream of `read_session_cookie` returning that UUID, so the
two things that matter here are that the signature is checked before the value is trusted, and
that the cookie actually reaches the browser.

WHY THE VERSION IS IN THERE: without a server-side session store there is otherwise no way to
revoke one cookie. A cookie signed over `household_id` alone stays good for its full 30 days
after a password change, so a stolen cookie or a lost laptop could only be dealt with by
rotating `SESSION_SECRET` — which signs *every* household out of *every* device. The version is
a per-household revocation counter: `households.session_version` is bumped, and every cookie
minted before the bump stops matching. See `app.deps.require_household`, which is the one place
the comparison happens.

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
# The signed payload is one short string: "<household_id>:<session_version>". A dict would cost
# ~30 more base64 characters per request for two fields whose order is never going to change,
# and the cookie rides on every single request the SPA makes.
_PAYLOAD_SEPARATOR = ":"
# A cookie whose payload is a bare UUID predates `households.session_version` and was minted
# when every household was, by definition, at version 1. Reading it as 1 rather than rejecting
# it means the deploy that adds this column does not sign the whole userbase out — and the
# first bump revokes those cookies exactly like any other.
LEGACY_SESSION_VERSION = 1
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


def set_session_cookie(response: Response, household_id: UUID, session_version: int) -> None:
    """Sign `household_id` and `session_version` into `penny_session` on this response.

    `session_version` is REQUIRED and has no default on purpose. A default would be a guess
    about a revocation counter: mint version 1 for a household that has already bumped to 2 and
    the family cannot log in at all, and mint "whatever is current" and the revocation is a
    silent no-op. Every caller has the `Household` row in hand — pass `household.session_version`.
    """
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _serializer().dumps(f"{household_id}{_PAYLOAD_SEPARATOR}{session_version}"),
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


def read_session_cookie(token: str | None, *, current_version: int | None = None) -> UUID | None:
    """The signed cookie value -> household id, or None.

    None for every failure mode there is — absent, tampered, signed with a rotated secret,
    older than SESSION_MAX_AGE_DAYS, not a UUID, or (when `current_version` is supplied)
    revoked. The caller cannot accidentally distinguish "expired" from "forged" from "signed
    out everywhere", because it should treat all of them identically: send them to /login. A
    revoked session that came back as a household id plus a reason is a session some future
    caller uses anyway.

    `current_version` is optional only because it cannot be known on the first call: the row
    that holds it is loaded BY the id this function returns. `require_household` therefore
    calls this twice — once to learn which household to load, once to check that household's
    version — and the second call is the one that matters. Verifying the signature twice costs
    one extra HMAC over ~100 bytes.
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

    raw_id, _, raw_version = str(raw).partition(_PAYLOAD_SEPARATOR)
    try:
        household_id = UUID(raw_id)
    except (ValueError, AttributeError, TypeError):
        logger.warning("security.session_payload_not_a_uuid")
        return None

    if raw_version:
        try:
            version = int(raw_version)
        except ValueError:
            logger.warning("security.session_payload_version_not_an_int")
            return None
    else:
        version = LEGACY_SESSION_VERSION

    if current_version is not None and version != current_version:
        # Not a warning: this is what a password change or "sign out everywhere" is SUPPOSED to
        # do to every other device, so it is expected traffic, not an attack signal.
        logger.info("security.session_revoked", extra={"household_id": str(household_id)})
        return None
    return household_id
