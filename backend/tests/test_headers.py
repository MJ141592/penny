"""The four things about the security headers that would rot silently.

Not a test of every directive — the policy is allowed to change, and pinning the whole string
would only make it annoying to change. These are the properties whose loss is invisible: a
'unsafe-inline' added to script-src to make one library work, HSTS escaping to a dev server,
an operator-supplied URL becoming part of the policy, or the middleware quietly not being on
the response at all.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.headers import content_security_policy, install_security_headers, security_headers


def _settings(**kwargs: object) -> Settings:
    # _env_file=None and an explicit gowa_url: the developer's own .env must not decide
    # whether these assertions hold.
    kwargs.setdefault("gowa_url", None)
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_no_hsts_outside_production(env: str) -> None:
    """A dev server that sends HSTS pins the whole localhost origin in that browser for a year.

    It is not undone by fixing the code; the developer has to clear it in chrome://net-internals.
    """
    assert "strict-transport-security" not in dict(security_headers(_settings(env=env)))


def test_hsts_in_production() -> None:
    headers = dict(security_headers(_settings(env="production")))
    assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_script_src_never_relaxed() -> None:
    """The one directive that makes the header worth sending. 'unsafe-inline' here is the
    difference between a CSP and a decoration, and it is the first thing a stuck developer adds.
    """
    directives = dict(
        part.strip().split(" ", 1) for part in content_security_policy(_settings()).split(";")
    )
    assert directives["script-src"] == "'self'"
    # style-src too: React's style={{...}} goes through the CSSOM, which CSP does not police,
    # so 'unsafe-inline' here would buy nothing and unblock injected <style> for everyone.
    assert directives["style-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"


@pytest.mark.parametrize(
    "gowa_url",
    [
        "http://gowa; script-src *",  # a second directive smuggled in
        "http://gowa 'unsafe-inline'",  # a second source smuggled in
        "ftp://gowa",  # not a scheme a browser fetches images over
        "gowa.railway.internal:3000",  # no scheme, so urlsplit sees no host
    ],
)
def test_gowa_url_cannot_inject_into_the_policy(gowa_url: str) -> None:
    """GOWA_URL is operator-supplied and lands in a response header verbatim if we let it.

    A space or a `;` in the value is a second source or a second directive — `script-src *`
    smuggled into our own policy — so anything that is not a plain origin is dropped whole.
    """
    assert content_security_policy(_settings(gowa_url=gowa_url)) == content_security_policy(
        _settings()
    )


def test_gowa_origin_allowed_for_the_pairing_qr() -> None:
    """The pairing screen renders GOWA's QR PNG in an <img>; 'self' alone blocks it."""
    policy = content_security_policy(_settings(gowa_url="http://localhost:3000/some/path"))
    assert "img-src 'self' data: http://localhost:3000;" in policy


def test_headers_are_on_the_response() -> None:
    """Through a real ASGI stack, because the wiring is the part that can silently vanish."""
    app = FastAPI()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    install_security_headers(app, _settings(env="dev"))

    response = TestClient(app).get("/api/health")

    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers
