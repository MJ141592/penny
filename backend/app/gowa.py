"""HTTP client for the GOWA WhatsApp sidecar.

THE ONE RULE IN THIS FILE: **no method here ever raises into a handler.** The sidecar is a
separate container holding a websocket to WhatsApp's servers; it restarts on deploy, it loses
its session, it gets banned, it is simply not there in local development. None of that is an
error the family should see. Every call returns a typed value with `available=False` and a
short reason instead, so `GET /api/whatsapp/status` renders "not connected" and the feed — the
part of the product that has nothing to do with WhatsApp — keeps working.

That is why the return types are dataclasses rather than raw dicts: the unavailable case has to
be representable, and a `dict | None` return pushes that decision onto every call site.

Four verified facts that the code below encodes, each of which is silently wrong if you guess
(the first three from `docs/railway-deployment.md`):

* `/send/message` takes the field **`phone`** even for a group, and it wants the full
  `...@g.us` JID. There is no separate group endpoint.
  **UNVERIFIED on v9: whether `/send/*` also demands a `device_id`.** Every `/app/*` route on
  the deployed v9.0.0 image turned out to answer `400 {"code":"DEVICE_ID_REQUIRED"}` without
  one (2026-07-25), and `/send/*` was never re-tested after that discovery — GOWA has no public
  domain, so it cannot be probed from a developer machine. Guessing either way is a silent
  failure: send it unasked and a v9 that validates its body may reject an unknown field; omit
  it and every send may 400. So `send_message` does neither. It sends the documented body,
  and **only if GOWA answers `DEVICE_ID_REQUIRED`** does it resolve a device via `/devices` and
  retry with both the query parameter and the `X-Device-Id` header. That costs nothing when the
  guess-free path already works and self-heals when it does not.
* `/app/login` **blocks for up to 120 seconds** waiting for whatsmeow to produce the first QR.
  It gets its own timeout, and it must never be on a path anything polls.
* `qr_link` is a **PNG URL**, not a raw QR payload. Handing it to a QR renderer produces a QR
  code containing a URL, which scans as nothing.
* `GET /message/{message_id}/download` (verified in v9's `openapi.yaml`, 2026-07-25) is the
  **only** way to a media file's bytes. WhatsApp media is end-to-end encrypted, so the
  `mmg.whatsapp.net` URL in a stored payload is an encrypted blob and the `mediaKey` that
  opens it lives in GOWA's session store. `WHATSAPP_AUTO_DOWNLOAD_MEDIA` stays **false** — it
  grows `/app/statics/media` without bound — so `download_media` fetches on demand and the
  caller keeps the transcript, not the file.

Auth is HTTP basic from `settings.gowa_basic_auth` ("user:pass"). GOWA parses that variable
with `strings.Split(s, ":")` on its side, so the password provably contains no colon and
splitting on the first one here is exact, not a heuristic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

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
# Media download is the one call that moves real bytes, and it is not on a request path: it
# runs behind the webhook's 200, so it can afford to wait for a sidecar that has to fetch and
# decrypt the blob from WhatsApp before it answers. Still bounded — a stalled read here would
# otherwise pin a task for as long as the socket stayed open. `read` is the per-chunk budget,
# not the whole transfer, which is why it is far shorter than the pool-wide default would be.
MEDIA_TIMEOUT = httpx.Timeout(30.0, connect=5.0, read=20.0)

UNCONFIGURED = "gowa_not_configured"
UNREACHABLE = "gowa_unreachable"
# GOWA's own error code for "you did not tell me which device", promoted to a first-class
# reason because it is the one 400 a caller can actually recover from.
DEVICE_ID_REQUIRED = "gowa_device_id_required"
NO_DEVICE = "gowa_no_device_id"
DEVICE_ID_HEADER = "X-Device-Id"
# Media-only reasons. Each one means "there is no audio to transcribe", and each one is a
# different thing to go and look at, so they are not collapsed into UNREACHABLE.
MEDIA_TOO_LARGE = "gowa_media_too_large"
MEDIA_EMPTY = "gowa_media_empty"
MEDIA_NOT_BINARY = "gowa_media_not_binary"

# GOWA's `code` is a SCREAMING_SNAKE enum. Anything else in that field is treated as absent
# rather than passed on: the sibling `message` field can echo the text we just sent, and a
# reason string ends up in logs.
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


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


def _error_code(response: httpx.Response) -> str | None:
    """GOWA's machine-readable `code`, or None. NEVER the sibling `message` — see `_ERROR_CODE`."""
    try:
        body = response.json()
    except Exception:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.match(code) else None


async def _call(
    method: str,
    path: str,
    *,
    http_timeout: httpx.Timeout,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
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
            response = await client.request(method, path, json=json, params=params, headers=headers)
            response.raise_for_status()
            return _unwrap(response.json()), None
    except httpx.HTTPStatusError as exc:
        # Log the status and the enum code, never the body: a GOWA error body can echo the
        # message we just sent.
        code = _error_code(exc.response)
        log.warning(
            "gowa.http_error",
            extra={"path": path, "status": exc.response.status_code, "code": code},
        )
        if code == "DEVICE_ID_REQUIRED":
            return None, DEVICE_ID_REQUIRED
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


def _device_id(device: dict[str, Any]) -> str | None:
    """`/devices` calls it `id`; `/app/status` calls the same value `device_id`. Accept both."""
    return _as_str(device.get("device_id")) or _as_str(device.get("id"))


async def _first_device_id() -> str | None:
    """The paired device, or None if the sidecar is down or nothing is paired yet."""
    devices, _ = await list_devices()
    return _device_id(devices[0]) if devices else None


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

    device_id = _device_id(devices[0])
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

    VERIFIED against v9.0.0 by completing a real pairing on 2026-07-25 — and the guess this
    docstring used to hold was WRONG. `/app/login` does NOT create a device; it demands a device
    id like every other `/app/*` route, so a device must exist first via
    `POST /devices {"name": ...}`. Hence create-then-login rather than login-and-hope.
    `qr_duration` is 30 seconds, so callers should expect to re-request rather than treat one QR
    as a durable link.
    """
    devices, error = await list_devices()
    if devices is None:
        return GowaLogin(available=False, error=error)

    device_id = None
    if devices:
        # Re-pair reuses the existing device, so the household keeps one identity across
        # sessions rather than accumulating a dead placeholder per QR scan.
        device_id = _device_id(devices[0])
    else:
        created, error = await _call(
            "POST", "/devices", json={"name": "penny"}, http_timeout=STATUS_TIMEOUT
        )
        if created is None:
            return GowaLogin(available=False, error=error)
        device_id = _as_str(created.get("id")) or _as_str(created.get("device_id"))
    if not device_id:
        return GowaLogin(available=False, error="gowa_no_device_id")

    results, error = await _call(
        "GET", "/app/login", params={"device_id": device_id}, http_timeout=LOGIN_TIMEOUT
    )
    if results is None:
        return GowaLogin(available=False, device_id=device_id, error=error)
    duration = results.get("qr_duration")
    return GowaLogin(
        available=True,
        device_id=_as_str(results.get("device_id")),
        # A PNG URL. Render it as an <img src>, do not feed it to a QR encoder.
        qr_link=_as_str(results.get("qr_link")),
        qr_duration=int(duration) if isinstance(duration, int | float | str) and duration else None,
    )


async def send_message(chat_id: str, text: str) -> GowaSendResult:
    """Send to a group. The field is `phone` EVEN FOR A GROUP JID — pass the full `...@g.us`.

    The `DEVICE_ID_REQUIRED` retry is the module docstring's unverified case made harmless: we
    never guess whether v9's `/send/*` wants a device id, we let GOWA say so and then answer.
    One extra round trip on a path that would otherwise have failed outright.
    """
    body = {"phone": chat_id, "message": text}
    results, error = await _call("POST", "/send/message", http_timeout=SEND_TIMEOUT, json=body)

    if error == DEVICE_ID_REQUIRED:
        device_id = await _first_device_id()
        if not device_id:
            # Nothing is paired (or the sidecar just went away). Either way there is no device
            # to send from, and the caller's job is to degrade, not to retry.
            log.warning("gowa.send_no_device", extra={"chat_id": chat_id})
            return GowaSendResult(ok=False, error=NO_DEVICE)
        log.info("gowa.send_retry_with_device", extra={"chat_id": chat_id})
        # Query parameter AND header: v9 documents both spellings and which one `/send/*`
        # honours is exactly the thing that could not be verified from here.
        results, error = await _call(
            "POST",
            "/send/message",
            http_timeout=SEND_TIMEOUT,
            json=body,
            params={"device_id": device_id},
            headers={DEVICE_ID_HEADER: device_id},
        )

    if results is None:
        return GowaSendResult(ok=False, error=error)
    # chat_id is an identifier, not content; the text we sent is never logged.
    log.info("gowa.sent", extra={"chat_id": chat_id, "char_count": len(text)})
    return GowaSendResult(ok=True, message_id=_as_str(results.get("message_id")))


async def download_media(provider_message_id: str) -> tuple[bytes, str] | None:
    """`GET /message/{id}/download` — `(bytes, content_type)`, or None if there is no media.

    THIS IS THE ONLY WAY TO THE BYTES. WhatsApp media is end-to-end encrypted: the
    `mmg.whatsapp.net` URL sitting in the stored payload is an encrypted blob, and the
    `mediaKey` that would decrypt it lives in GOWA's session store, not ours. Fetching that URL
    directly returns ciphertext. So the sidecar being down means no audio — not a slower path
    to it.

    Fetched ON DEMAND and never persisted. `WHATSAPP_AUTO_DOWNLOAD_MEDIA` stays false because
    it grows `/app/statics/media` without bound; the caller transcribes, keeps the text and
    drops these bytes.

    Streamed with a hard `transcription_max_bytes` cap rather than `response.content`, so a
    response that lies about (or omits) its length still cannot be read into memory without
    limit. Like everything else in this module it NEVER raises: None is a normal answer.
    """
    path = f"/message/{quote(provider_message_id, safe='')}/download"
    media, error = await _download(path)

    if error == DEVICE_ID_REQUIRED:
        # Same self-healing shape as `send_message`: never guess whether v9 wants a device id
        # on this route, let GOWA say so and then answer. See that docstring for why.
        device_id = await _first_device_id()
        if not device_id:
            log.warning("gowa.media_no_device")
            return None
        log.info("gowa.media_retry_with_device")
        media, error = await _download(
            path, params={"device_id": device_id}, headers={DEVICE_ID_HEADER: device_id}
        )

    if media is None:
        # No message id, no content: an id is a handle, and the reason is an enum of ours.
        log.info("gowa.media_unavailable", extra={"error": error})
        return None
    audio, content_type = media
    log.info(
        "gowa.media_downloaded",
        extra={"byte_count": len(audio), "content_type": content_type},
    )
    return media


async def _download(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[tuple[bytes, str] | None, str | None]:
    """`_call`'s sibling for a binary body. Separate because `_call` parses JSON, and a JSON
    parse of an audio file is a `UnicodeDecodeError` reported as "sidecar unreachable"."""
    base = _base_url()
    if not base:
        return None, UNCONFIGURED
    limit = get_settings().transcription_max_bytes
    try:
        async with (
            httpx.AsyncClient(base_url=base, auth=_auth(), timeout=MEDIA_TIMEOUT) as client,
            client.stream("GET", path, params=params, headers=headers) as response,
        ):
            if response.status_code >= 400:
                # The error body is small and is the only place the DEVICE_ID_REQUIRED
                # enum lives, so it is read — but only after the status says it is an
                # error, never for a 200 that could be megabytes of audio.
                await response.aread()
                code = _error_code(response)
                log.warning(
                    "gowa.media_http_error",
                    extra={"status": response.status_code, "code": code},
                )
                if code == "DEVICE_ID_REQUIRED":
                    return None, DEVICE_ID_REQUIRED
                return None, f"gowa_http_{response.status_code}"

            content_type = _content_type(response)
            if content_type.startswith("application/json"):
                # A 200 carrying JSON is GOWA telling us something, not audio. Sending it
                # to a transcription endpoint would pay for a nonsense answer.
                await response.aread()
                return None, MEDIA_NOT_BINARY

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                # Refuse before reading a byte when the response says how big it is.
                return None, MEDIA_TOO_LARGE

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    # And refuse mid-stream when it did not, or lied.
                    return None, MEDIA_TOO_LARGE
            if not body:
                return None, MEDIA_EMPTY
            return (bytes(body), content_type), None
    except Exception as exc:
        log.warning("gowa.media_unreachable", extra={"exc_type": type(exc).__name__})
        return None, UNREACHABLE


def _content_type(response: httpx.Response) -> str:
    """The bare media type: `audio/ogg; codecs=opus` -> `audio/ogg`. Parameters are noise to
    the caller, which only needs something to name the upload part with."""
    raw = response.headers.get("content-type") or ""
    return raw.split(";")[0].strip().lower() or "application/octet-stream"


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
