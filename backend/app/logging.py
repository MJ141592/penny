"""Structured logging: one JSON object per line on stdout, which is what Railway captures.

THE RULE, stated twice in the plan: never log message text, prompt text or model output.
Log identifiers and sizes instead — `message_id`, `household_id`, `char_count`, token counts.
This is a health record belonging to somebody's mother; a log aggregator is not a place it
should ever exist.

`DENYLIST_KEYS` is that rule made reviewable in one place, and `RedactionFilter` enforces it on
the *handler*, so it applies to every logger in the process — ours, the OpenAI SDK's, httpx's —
rather than only where a developer remembered. It is a backstop, not permission to hand a
message body to `logger.info`.

Wiring (main.py owns both lines):

    configure_logging()
    app.middleware("http")(make_request_log_middleware())
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

# Any log field whose key normalises to one of these is dropped before it can be written.
# Exact match, never substring: `message_id`, `char_count` and `input_tokens` are exactly the
# fields we DO want, and they all contain a denylisted word.
DENYLIST_KEYS: frozenset[str] = frozenset(
    {
        # message and model content
        "text",
        "body",
        "message",
        "prompt",
        "input",
        "instructions",
        "output_text",
        "quote",
        # real fields in this codebase that carry verbatim content: `messages.payload` is the
        # raw provider JSON and `ExtractedEvent.quotes` is a list of message excerpts.
        "payload",
        "quotes",
        # credentials
        "password",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "secret",
    }
)

REDACTED = "[redacted]"
# A key-based rule cannot catch a body passed under an innocent name, so every other string is
# capped too: an unforeseen leak costs a sentence rather than a conversation.
MAX_VALUE_CHARS = 200
MAX_ITEMS = 20
MAX_DEPTH = 4

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_SCOPE_KEY = "penny_request_id"
# Railway probes the healthcheck continuously; at INFO it drowns everything else.
QUIET_PATHS = frozenset({"/api/health"})

# Attacker-controlled: an inbound request id goes straight into the log line.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_request_id: ContextVar[str | None] = ContextVar("penny_request_id", default=None)
_request_logger = logging.getLogger("app.request")

# Everything LogRecord sets itself; anything else on the record is a caller's `extra`.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def new_request_id() -> str:
    """Short enough for a family to read one out over the phone from a 500 page."""
    return uuid4().hex[:12]


def current_request_id() -> str | None:
    return _request_id.get()


def request_id_for(request: Request) -> str | None:
    """The id stamped on this request, readable from an exception handler.

    The generic-500 handler runs OUTSIDE the middleware stack — Starlette's ServerErrorMiddleware
    wraps everything — by which time the contextvar has been reset. The ASGI scope is the one
    thing both layers share, so the id lives there too.
    """
    return request.scope.get(REQUEST_ID_SCOPE_KEY) or current_request_id()


def _normalise(key: str) -> str:
    return key.strip("_").replace("-", "_").lower()


def _truncate(value: str) -> str:
    if len(value) <= MAX_VALUE_CHARS:
        return value
    return f"{value[:MAX_VALUE_CHARS]}…(+{len(value) - MAX_VALUE_CHARS} chars)"


def redact(value: Any, depth: int = 0) -> Any:
    """Drop denylisted keys at any nesting level; cap everything else."""
    if depth >= MAX_DEPTH:
        return _truncate(str(value))
    if isinstance(value, dict):
        return {
            key: REDACTED if _normalise(str(key)) in DENYLIST_KEYS else redact(item, depth + 1)
            for key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
    if value is None or isinstance(value, bool | int | float):
        return value
    # UUIDs, datetimes and Decimals stringify short; a model instance stringifies to its
    # contents, which is exactly the leak the cap exists for.
    return _truncate(str(value))


class RedactionFilter(logging.Filter):
    """The last thing between a log call and stdout. Attached to the handler, not a logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED or key.startswith("_"):
                continue
            record.__dict__[key] = REDACTED if _normalise(key) in DENYLIST_KEYS else redact(value)
        # The rendered message is the one field no key-based rule can inspect. Render it now
        # and cap it, so a careless logger.info("%s", body) leaks a sentence, not a thread.
        record.msg = _truncate(record.getMessage())
        record.args = ()
        return True


class RequestIdFilter(logging.Filter):
    """Stamp every record with the id of the request that caused it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        return True


class StdoutHandler(logging.StreamHandler):
    """Resolve sys.stdout at emit time rather than at construction.

    A handler that captures the stream object when it is built keeps writing to it after
    something replaces sys.stdout — gunicorn does, pytest's capture does — and the lines
    silently go nowhere.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


class JsonFormatter(logging.Formatter):
    """One line, one object. Extras land at the top level so they are queryable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            # Tracebacks stay: a 500 is unfixable without one, and stdout is ours — they must
            # never reach the *client*, which is app.errors' job. The consequence to respect is
            # that the final line is the exception message, so never interpolate a message body
            # into one: `raise ValueError(f"bad text: {text}")` puts it straight into the log.
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str | None = None) -> None:
    """Send JSON to stdout and nowhere else. Safe to call twice.

    Only handlers we installed are replaced, so pytest's capture handlers survive a call in a
    test and a second call (uvicorn reload) does not double every line.
    """
    resolved = (level or os.getenv("PENNY_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, StdoutHandler)]:
        root.removeHandler(existing)

    handler = StdoutHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(resolved)

    # uvicorn and gunicorn install their own plain-text handlers; let their records reach ours
    # instead, or Railway gets two formats interleaved and neither parses.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error", "gunicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers = []
        server_logger.propagate = True
    # uvicorn.access would log a second line per request, with the query string in it.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs one INFO line per outbound request containing the full URL, and the GOWA
    # endpoints carry group JIDs and phone numbers in the path.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _clean_request_id(candidate: str | None) -> str | None:
    if candidate and _SAFE_REQUEST_ID.match(candidate):
        return candidate
    return None


def make_request_log_middleware() -> Callable[
    [Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]
]:
    """One log line per request, and a request id that survives into the error handler.

    Wire with `app.middleware("http")(make_request_log_middleware())`.
    """

    async def request_log_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = _clean_request_id(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
        request.scope[REQUEST_ID_SCOPE_KEY] = request_id
        token = _request_id.set(request_id)
        started = time.perf_counter()
        # The path only, never request.url: a query string is caller-controlled content.
        fields = {"method": request.method, "path": request.url.path}
        try:
            response = await call_next(request)
        except Exception:
            fields |= {"status": 500, "duration_ms": _elapsed_ms(started)}
            # No exc_info here — app.errors logs the traceback with the same request_id.
            _request_logger.warning("http.request", extra=fields)
            raise
        else:
            fields |= {"status": response.status_code, "duration_ms": _elapsed_ms(started)}
            level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
            _request_logger.log(level, "http.request", extra=fields)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            _request_id.reset(token)

    return request_log_middleware


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
