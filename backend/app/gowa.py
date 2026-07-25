"""HTTP client for the GOWA WhatsApp sidecar.

THE ONE RULE IN THIS FILE: **no method here ever raises into a handler.** The sidecar is a
separate container holding a websocket to WhatsApp's servers; it restarts on deploy, it loses
its session, it gets banned, it is simply not there in local development. None of that is an
error the family should see. Every call returns a typed value with `available=False` and a
short reason instead, so `GET /api/whatsapp/status` renders "not connected" and the feed — the
part of the product that has nothing to do with WhatsApp — keeps working.

That is why the return types are dataclasses rather than raw dicts: the unavailable case has to
be representable, and a `dict | None` return pushes that decision onto every call site.

Three verified facts from `docs/railway-deployment.md` that the code below encodes, each of
which is silently wrong if you guess:

* `/send/message` takes the field **`phone`** even for a group, and it wants the full
  `...@g.us` JID. There is no separate group endpoint.
* `/app/login` **blocks for up to 120 seconds** waiting for whatsmeow to produce the first QR.
  It gets its own timeout, and it must never be on a path anything polls.
* `qr_link` is a **PNG URL**, not a raw QR payload. Handing it to a QR renderer produces a QR
  code containing a URL, which scans as nothing.

Auth is HTTP basic from `settings.gowa_basic_auth` ("user:pass"). GOWA parses that variable
with `strings.Split(s, ":")` on its side, so the password provably contains no colon and
splitting on the first one here is exact, not a heuristic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Short, because a request handler is waiting on these and the sidecar is on the private
# network — if it has not answered in a few seconds it is not going to.
STATUS_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
SEND_TIMEOUT = httpx.Timeout(10.0, connect=2.0)
# GOWA's /app/login blocks until whatsmeow emits the first QR, with a 120s internal timeout.
# A slow response here is expected behaviour, not a hang; anything under ~125s would time out
# on the *client* side of a call that was about to succeed.
LOGIN_TIMEOUT = httpx.Timeout(130.0, connect=5.0)

UNCONFIGURED = "gowa_not_configured"
UNREACHABLE = "gowa_unreachable"


@dataclass(frozen=True, slots=True)
class GowaStatus:
    """`/app/status`. `available` is about the sidecar; the booleans are about WhatsApp.

    The distinction matters to the UI: `available=False` means "we cannot tell you", while
    `available=True, is_logged_in=False` means "the session died, show the re-pair button".
    Collapsing them makes a sidecar restart look like a lost pairing and sends the family to
    fetch a phone for nothing.
    """

    available: bool
    is_connected: bool = False
    is_logged_in: bool = False
    device_id: str | None = None
    jid: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GowaLogin:
    """`/app/login`. `qr_link` is a URL to a PNG, served unauthenticated and deleted in ~30s."""

    available: bool
    device_id: str | None = None
    qr_link: str | None = None
    qr_duration: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GowaSendResult:
    """`/send/message`. Outbound is best-effort in v1; nothing retries on our side."""

    ok: bool
    message_id: str | None = None
    error: str | None = None


def _auth() -> httpx.BasicAuth | None:
    raw = get_settings().gowa_basic_auth
    if not raw or ":" not in raw:
        return None
    user, _, password = raw.partition(":")
    return httpx.BasicAuth(user, password)


def _base_url() -> str | None:
    url = get_settings().gowa_url
    return url.rstrip("/") if url else None


def _unwrap(body: Any) -> dict[str, Any]:
    """GOWA answers `{"code": "SUCCESS", "message": ..., "results": {...}}`.

    Tolerate a bare object too — the envelope is a v9 convention, not a guarantee across the
    pinned-tag bumps this deployment expects to do roughly monthly.
    """
    if not isinstance(body, dict):
        return {}
    results = body.get("results")
    return results if isinstance(results, dict) else body


async def _call(
    method: str,
    path: str,
    *,
    http_timeout: httpx.Timeout,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """One request. Returns `(results, None)` or `(None, reason)` — never raises.

    The bare `except Exception` is deliberate and is the whole point of the module: an httpx
    version bump that introduces a new exception type, a DNS failure, a TLS error, a body that
    is not JSON — every one of those must degrade to "not connected" rather than 500 a page.
    """
    base = _base_url()
    if not base:
        return None, UNCONFIGURED
    try:
        async with httpx.AsyncClient(base_url=base, auth=_auth(), timeout=http_timeout) as client:
            response = await client.request(method, path, json=json, params=params)
            response.raise_for_status()
            return _unwrap(response.json()), None
    except httpx.HTTPStatusError as exc:
        # Log the status, never the body: a GOWA error body can echo the message we just sent.
        log.warning("gowa.http_error", extra={"path": path, "status": exc.response.status_code})
        return None, f"gowa_http_{exc.response.status_code}"
    except Exception as exc:
        log.warning("gowa.unreachable", extra={"path": path, "exc_type": type(exc).__name__})
        return None, UNREACHABLE


async def list_devices() -> tuple[list[dict[str, Any]] | None, str | None]:
    """`GET /devices` — the only device endpoint v9 serves without a device id.

    Verified against the deployed v9.0.0 image: every `/app/*` route answers
    `400 {"code":"DEVICE_ID_REQUIRED"}` unless you pass `device_id` as a query parameter or
    `X-Device-Id` header. Before the first pairing there is no id to pass, so `/app/status`
    is unreachable by construction and `/devices` is the only way to ask "is anything paired?".
    `results` is `null`, not `[]`, when nothing is.
    """
    results, error = await _call("GET", "/devices", http_timeout=STATUS_TIMEOUT)
    if error is not None:
        return None, error
    devices = results.get("results") if isinstance(results, dict) else None
    return ([d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []), None


async def get_status() -> GowaStatus:
    """Link health. Called by `/api/whatsapp/status`, which the settings screen polls at 30s.

    Two hops, because v9 split them: `/devices` to learn whether anything is paired, then
    `/app/status?device_id=...` for that device's live connection state. Collapsing the
    "sidecar is down" and "sidecar is up, nothing paired yet" cases into one `available=False`
    sends someone debugging private networking when the real answer is that nobody has scanned
    the QR code.
    """
    devices, error = await list_devices()
    if devices is None:
        return GowaStatus(available=False, error=error)
    if not devices:
        # The sidecar answered — it is healthy, there is simply no session to report on.
        return GowaStatus(available=True, is_connected=False, is_logged_in=False)

    device_id = _as_str(devices[0].get("device_id")) or _as_str(devices[0].get("id"))
    results, error = await _call(
        "GET", "/app/status", params={"device_id": device_id}, http_timeout=STATUS_TIMEOUT
    )
    if results is None:
        return GowaStatus(available=False, device_id=device_id, error=error)
    return GowaStatus(
        available=True,
        is_connected=bool(results.get("is_connected")),
        is_logged_in=bool(results.get("is_logged_in")),
        device_id=_as_str(results.get("device_id")) or device_id,
        jid=_as_str(results.get("jid")),
    )


async def start_login() -> GowaLogin:
    """Request a pairing QR. BLOCKS for up to ~120s — never call this from a polling path.

    UNVERIFIED against v9, deliberately: confirming it means completing a real pairing with a
    physical phone, which is the M1b spike. Every other `/app/*` route on the deployed image
    demands a device id, but `/app/login` is the route that CREATES a device, so it cannot
    require one for the first pairing. We therefore send the id only for a re-pair, where one
    exists, and fall back to no id. If v9 turns out to want a device created first, this is
    where it will show up — the error body says exactly what is missing.
    """
    devices, _ = await list_devices()
    device_id = None
    if devices:
        device_id = _as_str(devices[0].get("device_id")) or _as_str(devices[0].get("id"))
    params = {"device_id": device_id} if device_id else None
    results, error = await _call("GET", "/app/login", params=params, http_timeout=LOGIN_TIMEOUT)
    if results is None:
        return GowaLogin(available=False, error=error)
    duration = results.get("qr_duration")
    return GowaLogin(
        available=True,
        device_id=_as_str(results.get("device_id")),
        # A PNG URL. Render it as an <img src>, do not feed it to a QR encoder.
        qr_link=_as_str(results.get("qr_link")),
        qr_duration=int(duration) if isinstance(duration, int | float | str) and duration else None,
    )


async def send_message(chat_id: str, text: str) -> GowaSendResult:
    """Send to a group. The field is `phone` EVEN FOR A GROUP JID — pass the full `...@g.us`."""
    results, error = await _call(
        "POST",
        "/send/message",
        http_timeout=SEND_TIMEOUT,
        json={"phone": chat_id, "message": text},
    )
    if results is None:
        return GowaSendResult(ok=False, error=error)
    # chat_id is an identifier, not content; the text we sent is never logged.
    log.info("gowa.sent", extra={"chat_id": chat_id, "char_count": len(text)})
    return GowaSendResult(ok=True, message_id=_as_str(results.get("message_id")))


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
