"""One error envelope, no existence oracle, no leaked internals.

`docs/api-contract.md` promises that every non-2xx body is exactly `{"detail": "<sentence>"}`
with `detail` always a string, so the client can `toast(body.detail)` without type-narrowing.
FastAPI does not do that by default — `RequestValidationError` renders a *list* — so the
handlers here are what make the promise true.

Two rules from the plan are enforced structurally rather than by convention, because both fail
silently and neither shows up in a test of the happy path:

1. **404, never 403.** A 403 confirms the row exists somewhere, which is an existence oracle
   across households. Rather than trusting every future route to raise the right thing, the
   HTTP handler *collapses* 403 to 404 and renders one fixed sentence for every 404 — so a
   cross-tenant id and a random uuid come back byte-identical.
2. **An unhandled exception tells the client nothing.** No traceback, no exception message, no
   type name — just a generic sentence and a correlation id that appears verbatim in the log
   line, so an operator can find the traceback that the family never saw.

`install_error_handlers(app)` is called from main.py; nothing here imports the app.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import REQUEST_ID_HEADER, new_request_id, request_id_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# The one sentence every 404 renders, whoever raised it and for whatever reason.
NOT_FOUND_DETAIL = "Not found."
INTERNAL_ERROR_DETAIL = "Something went wrong on our end. Reference {request_id}."
# Enough to fix a form, short enough to stay a sentence.
MAX_VALIDATION_PROBLEMS = 3


class PennyError(Exception):
    """An error that already knows its status code and its client-safe sentence."""

    status_code = 500
    detail = "Something went wrong."

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class NotFoundError(PennyError):
    """Missing, or belonging to another household — deliberately indistinguishable.

    Takes no message. A caller able to pass one could write "that event belongs to the Shaws",
    and the whole point of returning 404 for cross-tenant access is that the response carries
    no information about what exists.
    """

    status_code = 404
    detail = NOT_FOUND_DETAIL

    def __init__(self) -> None:
        super().__init__()


class ConflictError(PennyError):
    """409 — duplicate import, group already linked."""

    status_code = 409
    detail = "That conflicts with something that already exists."


class ValidationError(PennyError):
    """422 — a request the schema accepted but the domain rejects (an unknown timezone)."""

    status_code = 422
    detail = "That request was not valid."


class UnauthorizedError(PennyError):
    """401 — not signed in, bad credentials, or a bad webhook signature."""

    status_code = 401
    detail = "Not signed in."


class BudgetExceededError(PennyError):
    """The spend guard refused the work. 409, per the contract: there is no 402 in this API.

    Defined here rather than in the LLM gateway, so the gateway, the import route and the cron
    tick all raise the same class and it renders one way. Not a `ConflictError` subclass: code
    catching duplicate-import conflicts must not silently swallow a budget refusal.
    """

    status_code = 409
    detail = "That would go over the spending limit."


def install_error_handlers(app: FastAPI) -> None:
    """Register the four handlers that own every non-2xx body in the app."""
    app.add_exception_handler(PennyError, _handle_penny_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)


def _envelope(status_code: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


async def _handle_penny_error(request: Request, exc: PennyError) -> JSONResponse:
    return _envelope(exc.status_code, exc.detail)


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 403 collapses to 404 HERE rather than at every call site: an existence oracle is one
    # forgotten `raise HTTPException(403)` away, and this is the only place that cannot forget.
    # Every 404 renders the same sentence too, so an unrouted path, a missing row and another
    # household's row are one indistinguishable response.
    if exc.status_code in (403, 404):
        if exc.status_code == 403:
            logger.warning("errors.forbidden_collapsed", extra={"path": request.url.path})
        return _envelope(404, NOT_FOUND_DETAIL)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    # Retry-After on a 429 and WWW-Authenticate on a 401 are part of the contract.
    return _envelope(exc.status_code, detail, headers=exc.headers)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _envelope(422, _flatten(exc.errors()))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """The only handler the client can reach without a developer having thought about it."""
    request_id = request_id_for(request) or new_request_id()
    logger.error(
        "errors.unhandled",
        exc_info=exc,
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "exc_type": type(exc).__name__,
        },
    )
    # Nothing derived from `exc` goes into the response — not the message, not the type. The
    # id is the only bridge between what the family sees and what is in the log.
    return _envelope(
        500,
        INTERNAL_ERROR_DETAIL.format(request_id=request_id),
        headers={REQUEST_ID_HEADER: request_id},
    )


def _flatten(errors: Sequence[Any]) -> str:
    """Pydantic's list of error dicts, as one renderable sentence.

    `err["input"]` is never included: for this app the rejected value is a password or a
    message body as often as it is a typo'd timezone.
    """
    problems = []
    for err in errors[:MAX_VALIDATION_PROBLEMS]:
        location = ".".join(
            str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path")
        )
        message = err.get("msg", "Invalid value")
        problems.append(f"{location}: {message}" if location else message)
    if not problems:
        return "That request was not valid."
    remaining = len(errors) - MAX_VALIDATION_PROBLEMS
    if remaining > 0:
        problems.append(f"and {remaining} more")
    return "; ".join(problems).rstrip(".") + "."
