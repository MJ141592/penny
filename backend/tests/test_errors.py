"""The error envelope, the no-403 rule, and the no-leak rule. All three fail silently."""

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import (
    NOT_FOUND_DETAIL,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
    install_error_handlers,
)
from app.logging import REQUEST_ID_HEADER, make_request_log_middleware

# The kind of string that must never come back to a browser.
LEAKY_MESSAGE = "Margaret fell over on Tuesday; event 7 belongs to household The Shaws"


class Credentials(BaseModel):
    username: str
    password: str


def build_app(*, with_request_ids: bool = False) -> FastAPI:
    app = FastAPI()
    if with_request_ids:
        app.middleware("http")(make_request_log_middleware())
    install_error_handlers(app)

    @app.get("/missing")
    def _missing() -> None:
        raise NotFoundError()

    @app.get("/other-household")
    def _other_household() -> None:
        # What a route does when the row exists but belongs to someone else.
        raise NotFoundError()

    @app.get("/forbidden")
    def _forbidden() -> None:
        # The mistake this module exists to make harmless.
        raise HTTPException(403, LEAKY_MESSAGE)

    @app.get("/conflict")
    def _conflict() -> None:
        raise ConflictError("This export has already been imported.")

    @app.get("/unauthorized")
    def _unauthorized() -> None:
        raise UnauthorizedError("Invalid username or password.")

    @app.get("/invalid")
    def _invalid() -> None:
        raise ValidationError("'Europe/Londn' is not a known timezone.")

    @app.get("/over-budget")
    def _over_budget() -> None:
        raise BudgetExceededError("That import would cost about $31, over the $25 limit.")

    @app.get("/rate-limited")
    def _rate_limited() -> None:
        raise HTTPException(
            429, "Too many attempts. Try again in a minute.", headers={"Retry-After": "60"}
        )

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError(LEAKY_MESSAGE)

    @app.post("/login")
    def _login(credentials: Credentials) -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def errors_client() -> TestClient:
    # raise_server_exceptions=False: assert on the 500 the client sees, not on the traceback
    # the test runner would otherwise re-raise.
    return TestClient(build_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("/missing", 404, NOT_FOUND_DETAIL),
        ("/conflict", 409, "This export has already been imported."),
        ("/unauthorized", 401, "Invalid username or password."),
        ("/invalid", 422, "'Europe/Londn' is not a known timezone."),
        ("/over-budget", 409, "That import would cost about $31, over the $25 limit."),
        ("/rate-limited", 429, "Too many attempts. Try again in a minute."),
    ],
)
def test_errors_render_the_documented_envelope(
    errors_client: TestClient, path: str, status: int, detail: str
) -> None:
    response = errors_client.get(path)
    assert response.status_code == status
    # Exactly one key, and it is always a string: the client never type-narrows.
    assert response.json() == {"detail": detail}


def test_default_details_are_used_when_the_caller_passes_none() -> None:
    assert BudgetExceededError().detail == "That would go over the spending limit."
    assert ConflictError().detail != ConflictError("custom").detail
    # A per-instance detail must not rewrite the class default for the next raiser.
    assert ConflictError().detail == "That conflicts with something that already exists."


def test_forbidden_collapses_to_404(errors_client: TestClient) -> None:
    response = errors_client.get("/forbidden")
    assert response.status_code == 404
    assert LEAKY_MESSAGE not in response.text


def test_403_404_and_an_unrouted_path_are_byte_identical(errors_client: TestClient) -> None:
    """No existence oracle: the response cannot tell you whether the row exists."""
    missing = errors_client.get("/missing")
    other = errors_client.get("/other-household")
    forbidden = errors_client.get("/forbidden")
    unrouted = errors_client.get("/no-such-route")
    bodies = {missing.content, other.content, forbidden.content, unrouted.content}
    assert len(bodies) == 1
    assert {missing.status_code, other.status_code, forbidden.status_code} == {404}


def test_no_403_reaches_the_client() -> None:
    app = build_app()
    client = TestClient(app, raise_server_exceptions=False)
    statuses = {
        client.get(path).status_code
        for path in ("/missing", "/forbidden", "/conflict", "/unauthorized", "/invalid", "/boom")
    }
    assert 403 not in statuses


def test_header_bearing_errors_keep_their_headers(errors_client: TestClient) -> None:
    response = errors_client.get("/rate-limited")
    assert response.headers["retry-after"] == "60"


def test_validation_errors_flatten_to_one_sentence(errors_client: TestClient) -> None:
    response = errors_client.post("/login", json={"username": "the-shaws"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "password" in detail
    assert detail.endswith(".")


def test_validation_errors_never_echo_the_submitted_value(errors_client: TestClient) -> None:
    """Pydantic puts the rejected input in every error dict; here that is a password."""
    response = errors_client.post(
        "/login", json={"username": 5, "password": "correct-horse-battery-staple"}
    )
    assert response.status_code == 422
    assert "correct-horse-battery-staple" not in response.text


def test_unhandled_exception_leaks_nothing(errors_client: TestClient) -> None:
    response = errors_client.get("/boom")
    assert response.status_code == 500
    assert set(response.json()) == {"detail"}
    for leak in (LEAKY_MESSAGE, "Margaret", "RuntimeError", "Traceback", "test_errors.py"):
        assert leak not in response.text


def test_the_500_carries_a_correlation_id_that_is_logged(
    errors_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = errors_client.get("/boom")

    request_id = response.headers[REQUEST_ID_HEADER]
    assert request_id in response.json()["detail"]

    (record,) = [r for r in caplog.records if r.getMessage() == "errors.unhandled"]
    assert record.request_id == request_id
    # The traceback the client did not get has to exist somewhere.
    assert record.exc_info is not None


def test_the_correlation_id_is_the_one_the_middleware_stamped() -> None:
    """The 500 handler runs outside the middleware, so the id has to survive on the scope."""
    client = TestClient(build_app(with_request_ids=True), raise_server_exceptions=False)
    response = client.get("/boom", headers={REQUEST_ID_HEADER: "railway-req-42"})
    assert "railway-req-42" in response.json()["detail"]
