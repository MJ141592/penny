"""The joins between M1, M2 and M3 — the places no single track owned.

Every test here spans two modules that were written by different people against the same
document. They are cheap, and they are the only thing standing between "each track's suite is
green" and "the pieces fit". Each one names the seam it guards in its docstring.

Offline like everything else: the LLM is a `FakeTransport`, the database is `models.py`'s
metadata rather than a connection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import get_args
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import errors, models
from app.config import Settings
from app.extraction.chunker import (
    CONTEXT_HANDLE,
    PRIMARY_HANDLE,
    build_chunks,
    handles_for,
    render_transcript,
)
from app.extraction.dedup import compute_dedup_key, human_dedup_key
from app.extraction.runner import as_chunk_messages, extract_messages, normalise_precision
from app.ingest.contract import InboundMessage
from app.ingest.whatsapp_txt import ExportOptions, parse_export
from app.llm import gateway as llm_gateway
from app.llm.schemas import ExtractedEvent, Precision
from tests.test_extraction_runner import (
    CARE_BRIEF,
    TZ,
    chunk_messages,
    completed,
    event,
    gateway_over,
    symptom,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "demo_family.txt"


@pytest.fixture(scope="module")
def export() -> list[InboundMessage]:
    """The real 2,000-message fixture, parsed exactly as the CLI parses it."""
    with FIXTURE.open(encoding="utf-8", errors="replace") as handle:
        messages, _ = parse_export(handle, ExportOptions(tz=TZ, dayfirst=True))
    return messages


# --- seam 1: parser -> chunker -> handle resolution ------------------------------------


def test_the_parser_emits_exactly_what_the_chunker_adapter_reads(
    export: list[InboundMessage],
) -> None:
    """SEAM 1. Every field `as_chunk_messages` reads is one the parser actually populates.

    `InboundMessage` deliberately has no id, so the adaptation is real rather than absent —
    but it must be the ONLY adaptation, and it must not silently read a field the parser
    never sets.
    """
    assert export, "the fixture must parse"
    adapted = as_chunk_messages(export)

    human = [m for m in export if m.message_type != "system"]
    assert len(adapted) == len(human)
    assert len(adapted) < len(export), "system lines must be dropped, not carried"

    for inbound, chunk_message in zip(human, adapted, strict=True):
        assert chunk_message.sent_at == inbound.sent_at
        assert chunk_message.sent_at.tzinfo is not None
        assert chunk_message.sender_display_name == inbound.sender_display_name
        assert chunk_message.text == inbound.text
        assert chunk_message.message_type == inbound.message_type
        assert chunk_message.source_ordinal == inbound.source_ordinal
    # Ids must be unique or two messages collapse into one handle.
    assert len({m.id for m in adapted}) == len(adapted)


def test_every_parsed_message_is_primary_in_exactly_one_chunk(
    export: list[InboundMessage],
) -> None:
    """SEAM 1. `messages.extracted_at` is only a sound cursor if this holds."""
    adapted = as_chunk_messages(export)
    chunks = build_chunks(adapted)

    primary = [m.id for chunk in chunks for m in chunk.primary]
    assert len(primary) == len(set(primary)), "a message appears as primary twice"
    assert set(primary) == {m.id for m in adapted}


def test_the_handles_the_model_is_shown_are_the_handles_the_runner_resolves(
    export: list[InboundMessage],
) -> None:
    """SEAM 1. Three places form `m17`; they must be one place, over the real fixture.

    If `render_transcript` printed a label `Chunk.handles` did not carry, every citation
    would be dropped as `no_valid_handle` and the run would return nothing — with no error
    anywhere to point at.
    """
    chunks = build_chunks(as_chunk_messages(export))
    assert len(chunks) > 1

    for chunk in chunks:
        by_handle = chunk.primary_by_handle()
        printed = {line[1 : line.index("]")] for line in render_transcript(chunk, TZ).splitlines()}

        assert printed == set(chunk.handles)
        assert set(by_handle) | chunk.context_handles() == set(chunk.handles)
        assert {h: m.id for h, m in by_handle.items()}.items() <= chunk.handles.items()
        assert not (set(by_handle) & chunk.context_handles())


async def test_a_citation_resolves_to_the_message_the_chunker_labelled(
    export: list[InboundMessage],
) -> None:
    """SEAM 1, end to end: a model citing `m3` gets message m3's real id, off the real file."""
    adapted = as_chunk_messages(export)[:40]
    chunk = build_chunks(adapted)[0]
    quote = (chunk.primary[2].text or "")[:40]

    gateway, _ = gateway_over(
        completed(event(source_message_handles=["m3"], quotes=[quote], occurred_at="2026-01-12"))
    )
    result = await extract_messages(adapted, care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)

    assert len(result.events) == 1
    record = result.events[0]
    assert record.source_message_ids == [chunk.handles["m3"]]
    assert record.source_message_ids == [chunk.primary[2].id]
    assert record.source_excerpts[0].message_id == chunk.primary[2].id


def test_handles_for_is_one_based_and_prefixed(export: list[InboundMessage]) -> None:
    """SEAM 1. The prompt tells the model `m1`, not `m0`; an off-by-one loses every event."""
    adapted = as_chunk_messages(export)[:5]
    assert list(handles_for(PRIMARY_HANDLE, adapted)) == ["m1", "m2", "m3", "m4", "m5"]
    assert list(handles_for(CONTEXT_HANDLE, adapted[:2])) == ["c1", "c2"]


# --- seam 2: dedup always uses the HOUSEHOLD timezone -----------------------------------


def _late_night_event() -> ExtractedEvent:
    """00:40 BST on the 15th is 23:40 UTC on the 14th — two different days, two different keys.

    The bucket is a LOCAL calendar day, so an event either side of local midnight is exactly
    where a UTC default stops being merely wrong and becomes detectable.
    """
    return ExtractedEvent.model_validate(
        symptom(occurred_at="2026-07-15T00:40+01:00", occurred_at_precision="datetime")
    )


def test_the_dedup_key_moves_with_the_timezone() -> None:
    """SEAM 2. If it did not, passing UTC by mistake would be undetectable."""
    late = _late_night_event()
    assert compute_dedup_key(late, ZoneInfo("Europe/London")) != compute_dedup_key(
        late, ZoneInfo("UTC")
    )


async def test_the_runner_keys_events_in_the_household_timezone() -> None:
    """SEAM 2. The runner's `tz` argument is the household's; nothing may substitute UTC."""
    late = _late_night_event()
    gateway, _ = gateway_over(completed(late.model_dump()))
    result = await extract_messages(chunk_messages(), care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)

    assert len(result.events) == 1
    assert result.events[0].dedup_key == compute_dedup_key(late, TZ)
    assert result.events[0].dedup_key != compute_dedup_key(late, ZoneInfo("UTC"))


def test_the_key_format_is_the_plan_s() -> None:
    """SEAM 2/3. `llm:` + sha256 hex, and `human:` for anything a person wrote."""
    key = compute_dedup_key(_late_night_event(), TZ)
    assert key.startswith("llm:")
    assert len(key) == len("llm:") + 64
    assert int(key.removeprefix("llm:"), 16) >= 0  # pure hex

    assert human_dedup_key(uuid4()).startswith("human:")


# --- seam 3: the dedup key and the events table agree -----------------------------------


def test_the_dedup_key_column_holds_every_key_this_codebase_can_produce() -> None:
    """SEAM 3. An unbounded Text column and a btree index that a 70-char key fits inside."""
    column = models.Event.__table__.c.dedup_key
    assert column.nullable is False
    # sa.Text has no length; a VARCHAR(n) here would be the failure mode worth catching.
    assert getattr(column.type, "length", None) is None

    longest = compute_dedup_key(_late_night_event(), TZ) + "#99"  # the sibling-key suffix
    assert len(longest) < 100
    assert len(human_dedup_key(uuid4())) < 100

    index = next(
        i for i in models.Event.__table__.indexes if i.name == "uq_events_household_dedup_key"
    )
    assert index.unique is True
    assert [c.name for c in index.columns] == ["household_id", "dedup_key"]


# --- seam 3b: ONE precision vocabulary reaches the database -----------------------------


def test_every_precision_the_model_can_emit_is_one_the_events_table_accepts() -> None:
    """SEAM 3b. The LLM says `datetime`/`date`; the CHECK constraint says `exact`/`day`.

    `normalise_precision` is the only bridge, and it must point AT the database. Mapping the
    other way produces a value that passes every test in this repo and fails the INSERT.
    """
    for precision in get_args(Precision):
        assert normalise_precision(precision) in models.OCCURRED_AT_PRECISIONS, precision

    # The contract's own spellings must survive unchanged, or a re-normalisation corrupts them.
    for precision in models.OCCURRED_AT_PRECISIONS:
        assert normalise_precision(precision) == precision
    assert normalise_precision(None) == "unknown"


async def test_a_run_s_records_carry_a_database_legal_precision() -> None:
    """SEAM 3b, at the point M4 will actually read it."""
    gateway, _ = gateway_over(completed(event(), symptom()))
    result = await extract_messages(chunk_messages(), care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)

    assert len(result.events) == 2
    for record in result.events:
        assert record.occurred_at_precision in models.OCCURRED_AT_PRECISIONS


# --- seam 4: the parser's output fits the messages table --------------------------------


def test_messages_has_a_column_for_every_field_the_parser_sets(
    export: list[InboundMessage],
) -> None:
    """SEAM 4. `InboundMessage` is frozen; `messages` must be able to hold all of it."""
    columns = models.Message.__table__.c
    for name in (
        "provider",
        "provider_message_id",
        "sender_wa_jid",
        "sender_wa_lid",
        "sender_display_name",
        "sent_at",
        "text",
        "message_type",
        "payload",
        "source_ordinal",
    ):
        assert name in columns, name

    providers = {m.provider for m in export}
    assert providers == {"whatsapp_export"}
    assert providers <= set(models.PROVIDERS), "the provider CHECK would reject these"


def test_every_message_type_and_ordinal_the_fixture_produces_is_storable(
    export: list[InboundMessage],
) -> None:
    """SEAM 4. `message_type` deliberately has NO check constraint — media types are open."""
    columns = models.Message.__table__.c
    assert not [
        c for c in models.Message.__table__.constraints if getattr(c, "name", "") == "message_type"
    ]

    kinds = {m.message_type for m in export}
    assert "text" in kinds and "system" in kinds
    assert len(kinds) > 2, "the fixture should exercise media types too"
    assert all(isinstance(k, str) and k for k in kinds)

    ordinals = [m.source_ordinal for m in export]
    assert all(isinstance(o, int) and 0 <= o < 2**31 for o in ordinals)
    assert columns.source_ordinal.nullable is True  # GOWA messages have no line number


def test_every_payload_is_json_serialisable_and_free_of_the_one_byte_jsonb_refuses(
    export: list[InboundMessage],
) -> None:
    """SEAM 4. `payload` is JSONB, and Postgres rejects U+0000 inside a JSON string."""
    for message in export:
        blob = json.dumps(message.payload)
        assert "\\u0000" not in blob
        assert "\x00" not in (message.text or "")
    assert all("raw" in m.payload for m in export), "the verbatim line must survive"


def test_a_nul_byte_in_an_export_never_reaches_text_or_payload() -> None:
    """SEAM 4. A wrong file uploaded as .txt must be a parse report, not a failed INSERT."""
    raw = "14/07/2026, 21:04 - Sarah: bad\x00night again\n"
    messages, _ = parse_export(StringIO(raw), ExportOptions(tz=TZ, dayfirst=True))

    assert len(messages) == 1
    assert "\x00" not in (messages[0].text or "")
    assert "\x00" not in json.dumps(messages[0].payload)
    assert "badnight again" in (messages[0].text or "")


# --- seam 9: /api/health must not reach the database ------------------------------------


def test_health_answers_when_the_database_is_unreachable() -> None:
    """SEAM 9. The property that matters: a Postgres blip must not fail the healthcheck.

    This used to assert that `app.main`'s import graph contained no `sqlalchemy`, which was a
    cheap proxy while main.py held two routes. It stopped being achievable the moment real
    routers were wired in — every one of them imports the models — and it was only ever a proxy
    anyway. What actually causes the Railway rollback loop is the health HANDLER issuing a query,
    so assert that directly, in a subprocess against an unroutable address so a stray connection
    would hang rather than quietly succeed against localhost.
    """
    probe = (
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "r = TestClient(app).get('/api/health');"
        "print('HEALTH=' + str(r.status_code) + r.text)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        # 10.255.255.1 is unroutable: a query would time out, not connect to a dev database.
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://nobody:nobody@10.255.255.1:5432/nope",
            "PENNY_TEST_DATABASE_URL": "",
        },
        check=True,
        timeout=60,
    )
    verdict = next(
        line for line in reversed(result.stdout.splitlines()) if line.startswith("HEALTH=")
    )
    assert verdict == 'HEALTH=200{"status":"ok"}', result.stdout


def test_health_returns_ok_without_a_database_url(
    client: TestClient, settings_override: Callable[..., Settings]
) -> None:
    """SEAM 9, behaviourally: no DATABASE_URL configured, still 200."""
    settings_override(database_url=None)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- seam 6: the three parameters gpt-5.5 rejects ---------------------------------------


async def test_no_kwarg_named_temperature_top_p_or_max_tokens_ever_reaches_the_sdk() -> None:
    """SEAM 6. Asserted on the real call kwargs, not by reading the source."""
    gateway, transport = gateway_over(completed(event()))
    await extract_messages(chunk_messages(), care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)

    assert transport.calls
    for call in transport.calls:
        assert not {"temperature", "top_p", "max_tokens"} & set(call)
        assert call["max_output_tokens"] > 0
        assert call["reasoning"]["effort"] in {"none", "low", "medium", "high", "xhigh"}
        assert call["text"]["format"]["strict"] is True


def test_a_gateway_budget_refusal_renders_as_the_contract_s_409() -> None:
    """SEAM A/B. Two same-named exception classes made a spend refusal a generic 500.

    `app.errors` and `app.llm.gateway` each defined `BudgetExceededError`. The gateway raises
    its own; the error handler only knows `app.errors`' — so the 409 and the sentence the
    contract promises would have arrived as "Something went wrong on our end."
    """
    assert issubclass(llm_gateway.BudgetExceededError, errors.BudgetExceededError)
    assert issubclass(llm_gateway.BudgetExceededError, llm_gateway.GatewayError)
    # A budget refusal must never be swallowed by a handler catching duplicate-import 409s.
    assert not issubclass(llm_gateway.BudgetExceededError, errors.ConflictError)

    refusal = llm_gateway.BudgetExceededError()
    assert refusal.status_code == 409
    assert refusal.detail == errors.BudgetExceededError.detail
    assert refusal.run_id is None  # still carries the audit-row id when the gateway sets one


async def test_safety_identifier_is_omitted_rather_than_sent_as_null() -> None:
    """The CLI and the eval have no household; an explicit null is a value the API may reject."""
    gateway, transport = gateway_over(completed(event()))
    await extract_messages(chunk_messages(), care_brief=CARE_BRIEF, tz=TZ, gateway=gateway)

    assert "safety_identifier" not in transport.calls[0]


async def test_a_household_still_gets_a_pseudonymous_safety_identifier() -> None:
    household_id = uuid4()
    gateway, transport = gateway_over(completed(event()))
    await extract_messages(
        chunk_messages(),
        care_brief=CARE_BRIEF,
        tz=TZ,
        gateway=gateway,
        household_id=household_id,
    )

    identifier = transport.calls[0]["safety_identifier"]
    assert identifier and str(household_id) not in identifier


# --- seam 7: nothing that reaches a log can reconstruct what was said --------------------


def test_no_prompt_or_message_text_is_interpolated_into_a_log_call() -> None:
    """SEAM 7. A grep, kept as a test: the rule is easy to break and invisible when broken."""
    from app.logging import DENYLIST_KEYS

    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("log.", "logger.")):
                continue
            if any(f".{key}" in stripped for key in ("text", "body", "input", "instructions")):
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert not offenders, offenders
    assert {"text", "body", "quote", "payload"} <= set(DENYLIST_KEYS)


# --- seam 10: the Dockerfile copies paths that exist ------------------------------------


def test_every_path_the_dockerfile_copies_is_present_in_the_tree() -> None:
    """SEAM 10. A COPY of a path that no longer exists fails the build, not the tests."""
    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8")

    copied: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        parts = [p for p in stripped.split()[1:] if not p.startswith("--")]
        copied.extend(parts[:-1])  # the last token is the destination

    assert copied, "the Dockerfile should copy something"
    for source in copied:
        assert (repo / source.rstrip("/")).exists(), source

    # The SPA build output is what app/spa.py serves from /app/static.
    assert "COPY --from=frontend /build/dist ./static" in dockerfile
    assert "app.main:app" in dockerfile


def test_the_dockerignore_cannot_bake_a_developer_s_env_file_into_the_image() -> None:
    """SEAM 10. `config.py` reads backend/.env too, and a bare `.env` rule misses it."""
    repo = Path(__file__).resolve().parents[2]
    rules = {
        line.strip()
        for line in (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "**/.env" in rules
    assert "**/.env.*" in rules
