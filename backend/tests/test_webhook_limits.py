"""The unauthenticated door refuses an oversized body *while* reading it, not afterwards.

Only the invariants that rot silently are here. A cap written as "read the body, then check its
length" passes every functional test ever written for this endpoint — the 200s, the 401s, the
onboarding suite — while allocating whatever a stranger sends. So what is pinned is not "a big
body gets a 413" but the three things that make the 413 worth anything:

  * the stream is ABANDONED mid-body, so the bytes past the cap are never held (and never even
    pulled off the transport),
  * `Content-Length` can only reject early, never authorise — a small header attached to a large
    body is still refused,
  * the cap runs BEFORE `verify_signature`, which is the whole point: an attacker with no secret
    must not be able to make us buffer first and authenticate second.

The ordering test is the one that would break under an innocent-looking refactor ("read the body
once at the top, tidy the cap into a dependency"), and it is the one that costs a worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.routers import webhooks
from app.routers.webhooks import (
    MAX_BODY_BYTES,
    PayloadTooLargeError,
    read_capped_body,
)

SECRET = "a-real-webhook-secret-not-the-published-default"
CHUNK = 64 * 1024


def make_request(
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    delivered: list[int] | None = None,
) -> Request:
    """A real `Request` fed by a real ASGI receive, so `read_capped_body` streams for real.

    `delivered` accumulates the size of every chunk the reader actually pulls, which is how the
    test can tell "stopped reading" from "read it all and complained".
    """
    pending: Iterator[bytes] = iter(chunks)

    async def receive() -> dict[str, object]:
        chunk = next(pending, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        if delivered is not None:
            delivered.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/whatsapp/webhook",
        "headers": headers or [],
    }
    return Request(scope, receive)  # type: ignore[arg-type]


def signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


@pytest.fixture
def webhook_settings(settings_override: Callable[..., Settings]) -> Settings:
    return settings_override(
        whatsapp_webhook_secret=SECRET,
        session_secret="x" * 64,
        database_url="postgresql://penny:penny@localhost:5432/penny",
    )


def test_oversized_body_is_abandoned_mid_stream() -> None:
    """The refusal lands while the upload is still arriving, not after it lands in memory."""
    # 8 MB offered in 64 KB chunks. A correct reader stops five chunks in; the bug reads 128.
    chunks = [b"x" * CHUNK] * 128
    delivered: list[int] = []
    request = make_request(chunks, delivered=delivered)

    with pytest.raises(PayloadTooLargeError) as excinfo:
        asyncio.run(read_capped_body(request))

    assert excinfo.value.status_code == 413
    # Everything past the chunk that crossed the cap was never pulled off the transport.
    assert sum(delivered) <= MAX_BODY_BYTES + CHUNK
    assert sum(delivered) < len(chunks) * CHUNK


def test_a_small_content_length_does_not_license_a_large_body() -> None:
    """The header is a hint. The running total is the enforcement."""
    request = make_request(
        [b"x" * CHUNK] * 128,
        headers=[(b"content-length", b"12")],
    )
    with pytest.raises(PayloadTooLargeError):
        asyncio.run(read_capped_body(request))


def test_a_body_at_the_cap_is_returned_byte_for_byte() -> None:
    """The bytes we verify are the bytes we accumulated — nothing re-serialised, nothing lost."""
    body = b"".join(bytes([i % 251]) for i in range(MAX_BODY_BYTES))
    request = make_request([body[i : i + CHUNK] for i in range(0, len(body), CHUNK)])
    assert asyncio.run(read_capped_body(request)) == body


def test_cap_is_enforced_before_the_signature_is_checked(
    client: TestClient,
    webhook_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated stranger cannot make us do the reading first and the auth second."""
    verified: list[int] = []
    real_verify = webhooks.verify_signature

    def spy(raw_body: bytes, header: str | None) -> None:
        verified.append(len(raw_body))
        real_verify(raw_body, header)

    monkeypatch.setattr(webhooks, "verify_signature", spy)

    body = b"x" * (5 * 1024 * 1024)
    response = client.post(
        "/api/whatsapp/webhook",
        content=body,
        headers=signed(body),  # a VALID signature, so only the cap can be refusing this
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "That request body was too large."}
    # Not "verify_signature returned False" — it was never reached at all.
    assert verified == []
