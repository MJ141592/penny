"""Proof that a message body cannot reach stdout, and that every line is parseable JSON."""

import json
import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.logging import (
    DENYLIST_KEYS,
    MAX_VALUE_CHARS,
    REDACTED,
    REQUEST_ID_HEADER,
    configure_logging,
    make_request_log_middleware,
)

# A real sentence from a real family, of the kind this whole module exists to keep out of logs.
BODY = "Mum was up 4 times overnight and said her chest hurt again this morning"
# stdlib logging refuses `extra={"message": ...}` — it collides with LogRecord's own field.
RESERVED_BY_STDLIB = {"message"}


@pytest.fixture
def stdout_log(capsys: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    """configure_logging() writes to sys.stdout, which capsys owns for the duration."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    configure_logging("DEBUG")
    yield capsys
    root.handlers[:] = handlers
    root.setLevel(level)


def records(capture: pytest.CaptureFixture[str], logger: str | None = None) -> list[dict]:
    lines = [line for line in capture.readouterr().out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    return [r for r in parsed if logger is None or r["logger"] == logger]


def test_the_denylist_is_the_reviewed_set() -> None:
    """Pinned deliberately: removing a key here is a data-leak change, not a refactor."""
    reviewed = {
        "text",
        "body",
        "message",
        "prompt",
        "input",
        "instructions",
        "output_text",
        "quote",
        "payload",
        "quotes",
        "password",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "secret",
    }
    assert reviewed == DENYLIST_KEYS


def test_a_log_call_carrying_a_message_body_does_not_emit_the_body(
    stdout_log: pytest.CaptureFixture[str],
) -> None:
    household_id, message_id = uuid4(), uuid4()
    logging.getLogger("app.ingest").info(
        "ingest.message",
        extra={
            "household_id": household_id,
            "message_id": message_id,
            "char_count": len(BODY),
            "text": BODY,  # the mistake
        },
    )

    raw = stdout_log.readouterr().out
    assert BODY not in raw
    assert "chest" not in raw

    record = json.loads(raw)
    assert record["text"] == REDACTED
    # The fields the plan says to log survive intact — the denylist matches keys exactly, so
    # message_id is not collateral damage from "message".
    assert record["message_id"] == str(message_id)
    assert record["household_id"] == str(household_id)
    assert record["char_count"] == len(BODY)


def test_token_counts_survive(stdout_log: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("app.llm").info(
        "llm.run", extra={"input_tokens": 5600, "output_tokens": 760, "model": "gpt-5.5"}
    )
    (record,) = records(stdout_log)
    assert (record["input_tokens"], record["output_tokens"]) == (5600, 760)


@pytest.mark.parametrize("key", sorted(DENYLIST_KEYS - RESERVED_BY_STDLIB))
def test_every_denylisted_key_is_redacted_at_the_top_level(
    stdout_log: pytest.CaptureFixture[str], key: str
) -> None:
    logging.getLogger("app.test").info("event", extra={key: BODY})
    raw = stdout_log.readouterr().out
    assert BODY not in raw
    assert json.loads(raw)[key] == REDACTED


@pytest.mark.parametrize("key", sorted(DENYLIST_KEYS))
def test_every_denylisted_key_is_redacted_when_nested(
    stdout_log: pytest.CaptureFixture[str], key: str
) -> None:
    logging.getLogger("app.test").info(
        "event", extra={"fields": {"outer": [{key: BODY}], "kept": "note"}}
    )
    raw = stdout_log.readouterr().out
    assert BODY not in raw
    assert "note" in raw


@pytest.mark.parametrize("key", ["TEXT", "Authorization", "_body", "api-key"])
def test_key_matching_ignores_case_and_separators(
    stdout_log: pytest.CaptureFixture[str], key: str
) -> None:
    logging.getLogger("app.test").info("event", extra={"fields": {key: BODY}})
    assert BODY not in stdout_log.readouterr().out


def test_an_unforeseen_key_is_capped_rather_than_dumped(
    stdout_log: pytest.CaptureFixture[str],
) -> None:
    """The denylist cannot know every name; the cap bounds what an unknown one can leak."""
    logging.getLogger("app.test").info("event", extra={"innocent_name": BODY * 20})
    (record,) = records(stdout_log)
    assert record["innocent_name"].startswith(BODY[:20])
    assert len(record["innocent_name"]) < MAX_VALUE_CHARS + 40


def test_the_rendered_message_is_capped_too(stdout_log: pytest.CaptureFixture[str]) -> None:
    """No key-based rule can inspect logger.info('%s', body)."""
    logging.getLogger("app.test").info("chat: %s", BODY * 20)
    (record,) = records(stdout_log)
    assert len(record["event"]) < MAX_VALUE_CHARS + 40


def test_every_line_is_one_json_object(stdout_log: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("app.test").warning("multi\nline\nevent")
    logging.getLogger("app.test").info("second")
    lines = [line for line in stdout_log.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["level"] == "WARNING"
    assert first["logger"] == "app.test"
    assert first["ts"].endswith("+00:00")


def test_tracebacks_are_kept_server_side(stdout_log: pytest.CaptureFixture[str]) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("app.test").exception("errors.unhandled")
    (record,) = records(stdout_log)
    assert "Traceback" in record["exc"]


def test_configure_logging_is_idempotent(stdout_log: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")
    configure_logging("DEBUG")
    logging.getLogger("app.test").info("once")
    assert len(records(stdout_log, logger="app.test")) == 1


def build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(make_request_log_middleware())

    @app.get("/api/feed")
    def _feed() -> dict[str, list]:
        logging.getLogger("app.feed").info("feed.served", extra={"count": 3})
        return {"events": []}

    return app


def test_the_middleware_stamps_a_request_id(stdout_log: pytest.CaptureFixture[str]) -> None:
    response = TestClient(build_app()).get("/api/feed?before=2026-07-01T00:00:00Z")
    assert response.status_code == 200

    logged = {r["logger"]: r for r in records(stdout_log)}
    request_line, feed_line = logged["app.request"], logged["app.feed"]
    assert request_line["status"] == 200
    assert request_line["path"] == "/api/feed"
    assert isinstance(request_line["duration_ms"], float)
    # Work logged deep inside the request inherits the id without being handed it.
    assert (
        feed_line["request_id"] == request_line["request_id"] == response.headers[REQUEST_ID_HEADER]
    )


def test_the_query_string_is_never_logged(stdout_log: pytest.CaptureFixture[str]) -> None:
    TestClient(build_app()).get("/api/feed?before=2026-07-01T00:00:00Z")
    assert "before=" not in stdout_log.readouterr().out


def test_an_inbound_request_id_is_honoured(stdout_log: pytest.CaptureFixture[str]) -> None:
    response = TestClient(build_app()).get(
        "/api/feed", headers={REQUEST_ID_HEADER: "railway-req-42"}
    )
    assert response.headers[REQUEST_ID_HEADER] == "railway-req-42"
    assert records(stdout_log, logger="app.request")[0]["request_id"] == "railway-req-42"


@pytest.mark.parametrize("hostile", ["req 42", "x" * 100, "a\nlevel=ERROR"])
def test_a_hostile_request_id_is_replaced(
    stdout_log: pytest.CaptureFixture[str], hostile: str
) -> None:
    """The header is attacker-controlled and lands in every log line for that request."""
    response = TestClient(build_app()).get("/api/feed", headers={REQUEST_ID_HEADER: hostile})
    assert response.headers[REQUEST_ID_HEADER] != hostile
    assert hostile not in stdout_log.readouterr().out
