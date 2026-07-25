"""Browser security headers: one policy, built once at startup, on every response.

Why a module rather than five `response.headers[...] =` lines: a header that is only on the
routes somebody remembered is not a policy. This wraps the whole ASGI app, so a route added
tomorrow is covered by default and the only way to lose the protection is to delete the line in
main.py.

Wiring — main.py owns the line, immediately after the CORS block and before the routers:

    install_security_headers(app)

**THE HEADER THAT CAN HURT YOU.** HSTS is sent ONLY when `env == "production"`. A dev server
answering http://localhost with `Strict-Transport-Security` pins *every* localhost port in that
browser to https for a year — every other project on the machine included — and the only cure is
a trip to chrome://net-internals/#hsts. `settings.env` is the guard; do not "simplify" it away.

THE CSP WAS DERIVED FROM THE BUILT OUTPUT, not from a template. What `frontend/dist` actually
contains, so a future change of shape can be checked against it:

* `index.html`: one `<script type="module" crossorigin src="/assets/index-<hash>.js">`, one
  `<link rel="stylesheet" crossorigin href="/assets/index-<hash>.css">`, one `/favicon.svg`.
  No inline `<script>`, no inline `<style>`, no `<base>`. Hence `script-src 'self'` with no
  hash, no nonce and no `'unsafe-inline'`.
* the bundle: no `eval(`, no `new Function`, no `new Worker`, no `data:` inlined assets.
* the stylesheet: zero `@font-face` and zero `url(...)` — the type stack is system fonts, so
  there is no font CDN to allow and `default-src 'self'` covers `font-src`.
* the source: 15 React `style={{...}}` attributes — which need no `'unsafe-inline'`, see
  `style-src` below — one `<img src>` whose URL comes from the GOWA sidecar, no `<iframe>`,
  no `<object>`, no `dangerouslySetInnerHTML`, and no SSR, so no markup with a style attribute
  in it is ever parsed.

KNOWN GAP: Starlette's ServerErrorMiddleware sits outside every user middleware and writes the
generic 500 envelope itself, so that one response carries none of these headers. It is
`{"detail": "..."}` as JSON with no markup, so what is lost there is `nosniff` on nine words.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# One year, and subdomains too: a cookie set by a MITM'd `*.pennyai.chat` would be sent to the
# apex, so leaving subdomains unprotected leaves session fixation open. No `preload`, though —
# that is an entry in a browser-vendor registry, slow and manual to undo, and it would commit
# every future pennyai.chat subdomain to TLS before one exists.
HSTS = "max-age=31536000; includeSubDomains"

# A CSP source expression is separated from the next by whitespace and from the next directive
# by `;`, so an operator-supplied GOWA_URL containing either would inject directives into our
# policy. Hosts are letters, digits, dots and hyphens, plus `[]:` for an IPv6 literal or a port;
# anything else and the origin is dropped rather than emitted.
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-\[\]:]+$")


def _gowa_origin(settings: Settings) -> str | None:
    """The sidecar's origin, if it is one a browser could load an image from.

    `GET /api/whatsapp/relink` returns `qr_link`, a URL to a PNG **on the GOWA host**, and
    settings.tsx renders it in an `<img>`. Under `img-src 'self'` that image is blocked, and the
    pairing screen — the one screen where a broken image means "you cannot connect WhatsApp" —
    shows nothing. So the configured origin is allowed, scheme+host+port only.

    Returns None when GOWA is unset (the normal state locally) or when the URL is not something
    that can safely become a CSP source, which keeps the policy narrow by default.
    """
    raw = (settings.gowa_url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    # Credentials are not permitted in a CSP source and would void it; GOWA's basic auth lives
    # in its own setting anyway, so anything before an `@` is dropped, not honoured.
    host = parts.netloc.rsplit("@", 1)[-1]
    if not _SAFE_HOST.match(host):
        return None
    return f"{parts.scheme}://{host}"


def content_security_policy(settings: Settings) -> str:
    """The policy, with the reason for each directive beside it."""
    img_src = "'self' data:"
    gowa = _gowa_origin(settings)
    if gowa:
        img_src = f"{img_src} {gowa}"

    directives = (
        # The floor. Every fetch directive not named below (media-src, worker-src,
        # manifest-src, and whatever CSP adds next) inherits same-origin-only from here.
        ("default-src", "'self'"),
        # One hashed module from /assets, nothing inline, no eval anywhere in the bundle.
        # This is the directive that makes the whole header worth sending: with it, injected
        # markup cannot execute.
        ("script-src", "'self'"),
        # No 'unsafe-inline', and the 15 React `style={{...}}` attributes in the app are fine
        # without it. That is not a guess — react-dom's setValueForStyles does `node =
        # node.style` and then assigns properties, i.e. it writes through the CSSOM, which CSP
        # has no hook into. Measured in headless Chrome against this policy: `el.style.marginTop
        # = '20px'` and `style.setProperty` both apply, while `setAttribute('style', ...)`, a
        # style attribute in parsed markup and an injected `<style>` element are all refused.
        # Parsed markup is exactly the XSS shape, so this costs the app nothing and buys the
        # real thing. Re-run that check before adding a CSS-in-JS library, which would need
        # a nonce rather than 'unsafe-inline'.
        ("style-src", "'self'"),
        # favicon and everything the feed renders are same-origin; `data:` is the fixtures
        # demo's inline QR placeholder; the GOWA origin is the real pairing QR (see above).
        ("img-src", img_src),
        # Redundant with default-src today, deliberately: this is the directive that stops an
        # injected script POSTing a family's health history to someone else, and it must not
        # quietly inherit a default-src that someone later widens. Every real fetch goes to
        # /api on this origin.
        ("connect-src", "'self'"),
        # No <object> or <embed> anywhere in the app. Plugin content is a legacy script-
        # execution route that an SPA never needs, and the review asked for it by name.
        ("object-src", "'none'"),
        # The app renders no iframes at all, so even a same-origin one is injected.
        ("frame-src", "'none'"),
        # Nothing may frame Penny. The session is a cookie and the UI has destructive buttons,
        # which is precisely the clickjacking setup.
        ("frame-ancestors", "'none'"),
        # index.html has no <base>; an injected one would repoint every relative asset URL,
        # including the module script, at an attacker's host.
        ("base-uri", "'none'"),
        # Every form here is fetch + preventDefault, so a cross-origin form action can only be
        # injected — and the login form is where the passphrase is typed.
        ("form-action", "'self'"),
    )
    return "; ".join(f"{name} {value}" for name, value in directives)


def security_headers(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The exact headers to add to every response, resolved once from settings."""
    headers = [
        ("content-security-policy", content_security_policy(settings)),
        # Stops a browser deciding our JSON error envelope is really HTML and rendering it.
        ("x-content-type-options", "nosniff"),
        # Full URL when we link to ourselves, origin only when leaving over https, nothing at
        # all when downgrading. Penny's paths carry household and event ids; those are not a
        # third party's business.
        ("referrer-policy", "strict-origin-when-cross-origin"),
        # frame-ancestors is the real control; this is the same answer for a browser too old
        # to implement it.
        ("x-frame-options", "DENY"),
    ]
    if settings.env == "production":
        # Read the module docstring before moving this line.
        headers.append(("strict-transport-security", HSTS))
    return tuple(headers)


class SecurityHeadersMiddleware:
    """Pure ASGI, not BaseHTTPMiddleware — the healthcheck has to stay trivial.

    `@app.middleware("http")` builds a BaseHTTPMiddleware, which opens an anyio task group and
    a pair of memory streams per request. For four constant headers that is the wrong trade:
    Railway probes /api/health continuously and it must never touch anything. Here the entire
    per-response cost is one pass over a tuple resolved at startup — about a microsecond,
    measured, with nothing conditional on the route and nothing read from settings.
    """

    def __init__(self, app: ASGIApp, headers: tuple[tuple[str, str], ...]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    # setdefault, not assignment: a response that deliberately set its own
                    # value keeps it, and installing this twice cannot duplicate a header.
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_security_headers(app: FastAPI, settings: Settings | None = None) -> None:
    """Wrap the app so every response carries the headers. One line, called from main.py."""
    app.add_middleware(
        SecurityHeadersMiddleware, headers=security_headers(settings or get_settings())
    )
