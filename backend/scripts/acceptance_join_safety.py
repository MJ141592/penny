"""THE ACCEPTANCE RUN FOR THE INCIDENT. Run it, read it, believe it — or don't ship.

Penny is paired to a REAL WhatsApp account that was already a member of several unrelated group
chats. Onboarding provisioned a household on ANY message from ANY unknown group, so the welcome
message — which contains a login and a password — went out to all of them at once. Someone in one
of those chats wrote: "It sent the message to all groups i'm in at the same time, but i'm not sure
why". Three households, two accidental, onboarding disabled in production.

This script is the evidence that it cannot happen again. It is not a unit test and it does not
mock the thing under test. It prints a TRANSCRIPT: every line marked OUTBOUND is a message a real
person would have read in their chat, and the last section of the run lists every one of them.

WHAT IS REAL HERE, AND WHY EACH PIECE HAD TO BE:

  the app         The real FastAPI app from `app.main`, through the real router, with real
                  HMAC-signed bodies over the real bytes. Nothing calls a handler directly.
  Postgres        Real, via DATABASE_URL. The join ledger's uniqueness guarantee is an
                  `ON CONFLICT` in Postgres; asserting it against a fake proves nothing.
  GOWA            A real HTTP server on a real socket, speaking v9's actual envelope. The
                  outbound path therefore runs `httpx`, `_call`, `_unwrap` and the
                  `DEVICE_ID_REQUIRED` retry for real. Monkeypatching `gowa.send_message`
                  would skip the entire module that does the sending.
  the quiet gate  NOT patched. Driven through the real `startup_quiet_period_seconds` setting,
                  so `in_startup_quiet_period()` and its config plumbing are under test too.
  the assistant   Real `answer_mention`, real prompt, real post-processing — only the OpenAI
                  transport is a `FakeTransport`. No network call is made to OpenAI, ever.
  extraction      Real `run_extraction_for_household` and the real cadence gate, same fake
                  transport. The gate's decision is read off the `RunSummary` it returns.

Usage:

    docker compose up -d
    export DATABASE_URL=postgresql://penny:penny@localhost:5432/penny
    cd backend && uv run alembic upgrade head
    uv run python -m scripts.acceptance_join_safety

Exits non-zero if any expectation fails. Every group id is freshly generated per run: the ledger
is permanent by design, so a reused id would be "already known" on the second run and the
genuine-join steps would go green for the wrong reason forever after.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.config
import app.llm.gateway
from app.config import Settings, get_settings
from app.db import to_asyncpg_url
from app.models import Household, KnownGroup, Message, WhatsappLink

SECRET = "an-acceptance-run-webhook-secret-not-the-published-default"
# The paired account, as it arrives on every webhook envelope's `device_id`. These digits are
# the ones that showed up in a real production mention: "@17473209317 who are you".
OWN_JID = "17473209317@s.whatsapp.net"
MODEL = "gpt-5-mini"

failures: list[str] = []
outbound: list[tuple[str, str]] = []


# --- output -------------------------------------------------------------------------------


def step(number: str, title: str) -> None:
    print(f"\n{number}. {title.upper()}")


def line(text: str = "") -> None:
    print(f"  {text}")


def check(ok: bool, description: str) -> None:
    """One expectation. A failure is recorded and the run keeps going — a transcript that stops
    at the first problem hides the other three."""
    if not ok:
        failures.append(description)
    line(f"{'PASS' if ok else 'FAIL'}  {description}")


# --- the GOWA sidecar, as a real HTTP server ----------------------------------------------


class GowaStub(BaseHTTPRequestHandler):
    """v9's `/send/message` and `/devices`, close enough to make `app.gowa` do real work.

    Every send is appended to the module-level `outbound`. THAT LIST IS THE FAMILY'S GROUP CHAT.
    """

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.split("?")[0] == "/send/message":
            outbound.append((str(body.get("phone")), str(body.get("message"))))
            self._json({"code": "SUCCESS", "results": {"message_id": f"stub-{len(outbound)}"}})
        else:
            self._json({"code": "NOT_FOUND"}, status=404)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/devices":
            self._json({"code": "SUCCESS", "results": {"results": [{"id": OWN_JID}]}})
        else:
            self._json({"code": "NOT_FOUND"}, status=404)

    def _json(self, body: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: Any) -> None:
        """Silence the default stderr access log; it would drown the transcript."""


class LogCapture(logging.Handler):
    """Every log record the whole run emits, kept so the transcript can be searched for secrets.

    A house rule that is only ever asserted by reading the code is a house rule that decays.
    This makes "we never log message text or a passphrase" an EXPECTATION: the run generates a
    real passphrase nobody chose, then greps every line it produced for it.

    Attached to the root logger at level 0 so it sees records the JSON formatter would filter,
    and it stores the raw `getMessage()` plus the `extra` dict — because a secret leaked through
    `extra={"text": ...}` would never appear in the format string.
    """

    def __init__(self) -> None:
        super().__init__(level=0)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra = {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_LOGRECORD_KEYS
            }
            self.lines.append(f"{record.name} {record.getMessage()} {extra}")
        except Exception:  # a broken log line must not break the run
            self.lines.append(f"{record.name} <unformattable>")

    def text(self) -> str:
        return "\n".join(self.lines)


_STANDARD_LOGRECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


@contextmanager
def capture_logs() -> Iterator[LogCapture]:
    handler = LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@contextmanager
def gowa_stub() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), GowaStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# --- settings, swappable mid-run ----------------------------------------------------------


class SettingsHolder:
    """One mutable cell every module's `get_settings` reads through.

    Modules bind `get_settings` at import time, so patching `app.config` alone would leave every
    importer still calling the real, `.env`-reading, lru_cached one. Patching them all to read a
    cell (rather than a captured value) is what lets the quiet-period window be changed between
    steps without re-importing the world — which is how the real gate, not a monkeypatched stub,
    ends up being the thing under test.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.settings = Settings(_env_file=None, **kwargs)
        real = get_settings
        for module in list(sys.modules.values()):
            if getattr(module, "get_settings", None) is real:
                module.get_settings = lambda: self.settings  # type: ignore[attr-defined]
        app.config.get_settings = lambda: self.settings  # type: ignore[assignment]

    def update(self, **kwargs: Any) -> None:
        self.settings = self.settings.model_copy(update=kwargs)


# --- signed webhook delivery --------------------------------------------------------------


def post(client: TestClient, body: dict[str, Any]) -> Any:
    """Sign the EXACT bytes we send — those bytes are what the handler verifies."""
    raw = json.dumps(body).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/whatsapp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


def message_body(chat_id: str, text: str, *, is_from_me: bool = False) -> dict[str, Any]:
    return {
        "event": "message",
        "device_id": OWN_JID,
        "payload": {
            "id": f"msg-{uuid4().hex[:12]}",
            "chat_id": chat_id,
            "timestamp": "2026-07-26T10:30:00Z",
            "is_from_me": is_from_me,
            "from": "447700900123@s.whatsapp.net",
            "from_lid": "251556368777322@lid",
            "from_name": "Sarah",
            "body": text,
        },
    }


def join_body(chat_id: str, event: str = "group.joined") -> dict[str, Any]:
    """A `group.joined` as GOWA builds it.

    This is EXACTLY the body whatsmeow also re-emits, once per group, during app-state sync for
    groups the account was already in. Nothing in it distinguishes the two cases — which is the
    whole reason the gates have to be circumstantial.
    """
    return {
        "event": event,
        "device_id": OWN_JID,
        "payload": {
            "chat_id": chat_id,
            "type": "join",
            "jids": [OWN_JID],
            "group_name": "Mum's care",
        },
    }


def show(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    line(f"<-- {response.status_code} {json.dumps(body)}")
    return body


# --- database reads, each on its own engine -----------------------------------------------


def query(fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """Run one read on its OWN engine, in its own loop.

    The app's engine lives in the TestClient's portal thread and its asyncpg connections are
    bound to that loop; borrowing it from this thread gives "attached to a different loop" at
    random, which would make this script flaky and therefore worthless.
    """

    async def run() -> Any:
        engine = create_async_engine(to_asyncpg_url(os.environ["DATABASE_URL"]))
        try:
            async with AsyncSession(engine) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def households_for(chat_id: str) -> list[UUID]:
    return list(
        query(
            lambda s: s.scalars(
                sa.select(WhatsappLink.household_id).where(
                    WhatsappLink.group_external_id == chat_id
                )
            )
        ).all()
    )


def message_count(household_id: UUID) -> int:
    return int(
        query(
            lambda s: s.scalar(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.household_id == household_id)
            )
        )
    )


def in_ledger(chat_id: str) -> bool:
    return bool(
        query(
            lambda s: s.scalar(
                sa.select(sa.func.count())
                .select_from(KnownGroup)
                .where(KnownGroup.group_external_id == chat_id)
            )
        )
    )


def unextracted_count(household_id: UUID) -> int:
    """Messages still waiting for the extractor — the number the cadence gate compares."""
    return int(
        query(
            lambda s: s.scalar(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.household_id == household_id, Message.extracted_at.is_(None))
            )
        )
    )


def drain(household_id: UUID) -> None:
    """Extract everything waiting, gate and all, so a step can start from a known backlog."""
    from app.extraction.service import run_extraction_for_household

    async def run(session: AsyncSession) -> None:
        await run_extraction_for_household(session, household_id, force=True)
        await session.commit()

    query(run)


def cleanup(chat_ids: list[str]) -> None:
    async def run(session: AsyncSession) -> None:
        await session.execute(
            sa.delete(Household).where(
                Household.id.in_(
                    sa.select(WhatsappLink.household_id).where(
                        WhatsappLink.group_external_id.in_(chat_ids)
                    )
                )
            )
        )
        await session.execute(
            sa.delete(KnownGroup).where(KnownGroup.group_external_id.in_(chat_ids))
        )
        await session.commit()

    query(run)


# --- the fake OpenAI transport ------------------------------------------------------------


def completed(payload: dict[str, Any]) -> dict[str, Any]:
    """One Responses API body, in the shape the SDK parses."""
    return {
        "id": f"resp_{uuid4().hex[:8]}",
        "created_at": 1784275920.0,
        "model": MODEL,
        "object": "response",
        "output": [
            {
                "id": "msg_0001",
                "content": [
                    {"annotations": [], "text": json.dumps(payload), "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
        "usage": {
            "input_tokens": 2_400,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 180,
            "output_tokens_details": {"reasoning_tokens": 90},
            "total_tokens": 2_580,
        },
    }


class ScriptedTransport:
    """Answers by SCHEMA NAME: `AssistantReply` gets a reply, anything else gets an empty
    `ExtractionResult`. Records every call, so "zero LLM calls" is an assertion, not a hope.

    A queue would not work here. The assistant and the extractor share one transport and both
    run on tasks detached from the request, so the ORDER they call in is a race; a fixture list
    would pop the wrong item roughly half the time. Routing on what was asked is the only stable
    way to fake both at once.

    The name is read from `kwargs["text"]["format"]["name"]`, which is where `LLMGateway` puts
    `spec.schema.__name__` — an exact discriminator rather than a substring guess at the prompt.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _schema_name(kwargs: dict[str, Any]) -> str:
        text = kwargs.get("text")
        if not isinstance(text, dict):
            return ""
        fmt = text.get("format")
        return str(fmt.get("name", "")) if isinstance(fmt, dict) else ""

    async def create(self, **kwargs: Any) -> Any:
        from openai.types.responses import Response

        self.calls.append(kwargs)
        if self._schema_name(kwargs) == "AssistantReply":
            body: dict[str, Any] = {
                "kind": "about_penny",
                "reply": "I'm Penny. I keep this group's timeline.",
            }
        else:
            body = {"events": [], "no_events_reason": "nothing clinical in this batch"}
        return Response.model_validate(completed(body))

    @property
    def assistant_calls(self) -> int:
        return sum(1 for c in self.calls if self._schema_name(c) == "AssistantReply")


def wait_for(predicate: Callable[[], bool], timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def stays_silent(seconds: float = 1.5) -> None:
    """A NEGATIVE about an ASYNCHRONOUS send, which needs time in which to be wrong.

    Asserting `outbound == []` the instant a 200 comes back proves nothing: the send being ruled
    out is scheduled on a task that has not run yet. So give it a window to misbehave in.
    """
    before = len(outbound)
    time.sleep(seconds)
    check(
        len(outbound) == before, f"nothing sent (waited {seconds}s for a send that must not come)"
    )


# --- the run ------------------------------------------------------------------------------


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("set DATABASE_URL (see the module docstring)", file=sys.stderr)
        return 2

    transport = ScriptedTransport()

    with gowa_stub() as gowa_url:
        holder = SettingsHolder(
            database_url=db_url,
            test_database_url=db_url,
            env="test",
            session_secret="acceptance-session-secret-long-enough-to-be-real",
            whatsapp_webhook_secret=SECRET,
            gowa_url=gowa_url,
            openai_api_key="sk-not-used-the-transport-is-faked",
            app_public_url="https://pennyai.chat",
            onboarding_enabled=True,
            serve_frontend=False,
            # The gate under test. Start the run INSIDE the window: a freshly booted process is
            # exactly the state in which the incident happened.
            startup_quiet_period_seconds=10_000.0,
            # Gate (c). 2s rather than the production 45s so the run finishes: the mechanism is
            # identical, only the clock is shorter. Every genuine welcome below is therefore
            # held for 2s and re-checked before it is sent.
            join_burst_window_seconds=2.0,
            extract_min_unextracted=6,
            extract_max_age_hours=100_000.0,
            llm_monthly_budget_usd_per_household=Decimal("15"),
        )
        app.llm.gateway.get_transport = lambda: transport  # type: ignore[assignment]

        from app.main import app as penny_app
        from app.routers import webhooks

        # The paired-number cache and the reply limiter are module globals.
        webhooks._own_jid = None
        webhooks._own_jid_expires_at = 0.0
        webhooks._reply_hits.clear()

        # Spy on extraction WITHOUT replacing it: the real function, the real cadence gate, the
        # real Postgres reads. We only record what it decided.
        runs: list[Any] = []
        real_extraction = webhooks.run_extraction_for_household

        async def recording_extraction(session: AsyncSession, household_id: UUID, **kw: Any) -> Any:
            summary = await real_extraction(session, household_id, **kw)
            runs.append(summary)
            return summary

        webhooks.run_extraction_for_household = recording_extraction  # type: ignore[assignment]

        stranger = f"120363{uuid4().hex[:14]}@g.us"  # a group Penny has sat in for months
        old_friend = f"120363{uuid4().hex[:14]}@g.us"  # chats first, then "joins"
        synced = f"120363{uuid4().hex[:14]}@g.us"  # joins during app-state replay
        genuine = f"120363{uuid4().hex[:14]}@g.us"  # the one real invitation
        chat_ids = [stranger, old_friend, synced, genuine]
        cleanup(chat_ids)

        try:
            with capture_logs() as capture, TestClient(penny_app) as client:
                run_transcript(
                    client, holder, transport, runs, capture, stranger, old_friend, synced, genuine
                )
        finally:
            webhooks.run_extraction_for_household = real_extraction  # type: ignore[assignment]
            cleanup(chat_ids)

    print("\n" + "=" * 88)
    print("EVERY OUTBOUND WHATSAPP MESSAGE THIS RUN PRODUCED")
    print("=" * 88)
    for chat, text in outbound:
        print(f"  -> {chat}\n     {text.splitlines()[0][:70]}...  ({len(text)} chars)")
    print(f"\n  total: {len(outbound)}")
    print(f"  groups written to: {sorted({c for c, _ in outbound})}")

    print("\n" + "=" * 88)
    if failures:
        print(f"FAILED — {len(failures)} expectation(s) did not hold:")
        for description in failures:
            print(f"  - {description}")
        return 1
    print("ALL EXPECTATIONS HELD")
    return 0


def run_transcript(
    client: TestClient,
    holder: SettingsHolder,
    transport: ScriptedTransport,
    runs: list[Any],
    capture: LogCapture,
    stranger: str,
    old_friend: str,
    synced: str,
    genuine: str,
) -> None:
    # --- 1 ---------------------------------------------------------------------------------
    step("1", "a message from an UNKNOWN group (this is the incident)")
    line(f"--> event=message  chat={stranger}")
    body = show(post(client, message_body(stranger, "anyone free thursday?")))
    check(body == {"status": "ignored", "reason": "unknown_group"}, "200 ignored/unknown_group")
    check(households_for(stranger) == [], "no household was provisioned")
    check(in_ledger(stranger), "the group WAS recorded in the ledger (inoculated for good)")
    stays_silent()

    # --- 2 ---------------------------------------------------------------------------------
    step("2", "group.joined INSIDE the startup quiet window (app-state sync)")
    line(f"    startup_quiet_period_seconds={holder.settings.startup_quiet_period_seconds}")
    line(f"--> event=group.joined  chat={synced}")
    body = show(post(client, join_body(synced)))
    check(body == {"status": "ignored", "reason": "sync_suppressed"}, "200 ignored/sync_suppressed")
    check(households_for(synced) == [], "no household")
    check(in_ledger(synced), "recorded anyway — so it can never provision later either")
    stays_silent()

    line("")
    line("...the quiet period ends, and the SAME join is delivered again:")
    holder.update(startup_quiet_period_seconds=0.0)
    body = show(post(client, join_body(synced)))
    check(body == {"status": "ignored", "reason": "already_known"}, "still refused: already_known")
    check(households_for(synced) == [], "still no household — permanently inoculated")
    stays_silent()

    # --- 3 ---------------------------------------------------------------------------------
    step("3", "group.joined for an ALREADY-SEEN group id")
    line("    (this group chats first — months of ordinary traffic — then 'joins')")
    show(post(client, message_body(old_friend, "did anyone feed the cat")))
    check(in_ledger(old_friend), "ordinary traffic put it in the ledger")
    line(f"--> event=group.joined  chat={old_friend}")
    body = show(post(client, join_body(old_friend)))
    check(body == {"status": "ignored", "reason": "already_known"}, "200 ignored/already_known")
    check(households_for(old_friend) == [], "no household")
    stays_silent()

    # --- 4 ---------------------------------------------------------------------------------
    step("4", "a GENUINE group.joined — unseen id, quiet period over, arriving ALONE")
    # Steps 1-3 put three groups into the ledger inside a couple of seconds, which is a sync
    # burst as far as gate (c) is concerned — and it refuses the join, correctly. A real
    # invitation arrives in a quiet moment, so wait for one. This pause is not the harness
    # covering something up; it is the shape of the guarantee: SOLITARY joins get a welcome,
    # and anything arriving in company does not.
    quiet_for = holder.settings.join_burst_window_seconds + 1.0
    line(f"    waiting {quiet_for}s for a quiet moment — gate (c) refuses joins in company")
    time.sleep(quiet_for)
    line(f"--> event=group.joined  chat={genuine}")
    body = show(post(client, join_body(genuine)))
    check(body == {"status": "ok", "onboarded": True}, "200 ok/onboarded")
    households = households_for(genuine)
    check(len(households) == 1, "exactly one household")
    check(wait_for(lambda: len(outbound) == 1), "exactly one welcome message was sent")
    if outbound:
        chat, welcome = outbound[0]
        check(chat == genuine, "the welcome went to the group that just added Penny")
        line("")
        line("OUTBOUND -> " + chat)
        for text_line in welcome.splitlines():
            line("  | " + text_line)
    household_id = households[0] if households else None

    # --- 5 ---------------------------------------------------------------------------------
    step("5", "that same join REPLAYED 4x (GOWA retries any non-2xx five times)")
    for attempt in range(1, 5):
        body = show(post(client, join_body(genuine)))
        check(
            body == {"status": "ignored", "reason": "already_known"},
            f"replay {attempt}: ignored/already_known",
        )
    check(len(households_for(genuine)) == 1, "STILL exactly one household")
    stays_silent()
    check(len(outbound) == 1, "STILL exactly one welcome message, total")

    if household_id is None:
        return

    # --- 6 ---------------------------------------------------------------------------------
    # --- 5b --------------------------------------------------------------------------------
    step("5b", "AN APP-STATE BURST THAT ARRIVES LATE — the gap gates (a) and (b) leave open")
    line("    Eight groups the account has belonged to for months. The ledger has never seen")
    line("    them (so gate (a) calls each one a first sighting) and the quiet window is over")
    line("    (so gate (b) is open). This is production's exact state on the first deploy, and")
    line("    against the code as first written it produced 8 households and 8 passwords.")
    before = len(outbound)
    burst = [f"120363{uuid4().hex[:14]}@g.us" for _ in range(8)]
    try:
        for chat_id in burst:
            reason = show(post(client, join_body(chat_id))).get("reason", "onboarded")
            line(f"    {chat_id[:20]}... -> {reason}")
        # Longer than the burst window, so a welcome that is merely SLOW cannot pass for one
        # that was withheld: by now the held first join has woken, looked again and seen 8.
        time.sleep(6.0)
        check(len(outbound) == before, "NOTHING was sent to any of the eight groups")
        provisioned = [g for g in burst if households_for(g)]
        check(
            len(provisioned) <= 1,
            f"at most one household from the whole burst (got {len(provisioned)}) — "
            "an orphan row is deletable, a password is not",
        )
    finally:
        cleanup(burst)

    # --- 6 ---------------------------------------------------------------------------------
    step("6", "an @-mention in the LINKED group -> a reply is produced")
    before = len(outbound)
    line("--> event=message  body='@17473209317 who are you'")
    body = show(post(client, message_body(genuine, "@17473209317 who are you")))
    check(body.get("reply") == "scheduled", "the handler scheduled a reply")
    check(wait_for(lambda: len(outbound) > before), "a reply was sent")
    if len(outbound) > before:
        chat, reply = outbound[-1]
        check(chat == genuine, "the reply went to the group that asked")
        line(f"OUTBOUND -> {chat}")
        for text_line in reply.splitlines():
            line("  | " + text_line)
    check(transport.assistant_calls >= 1, "the real assistant ran (over a faked transport)")

    # --- 7 ---------------------------------------------------------------------------------
    step("7", "an @-mention in an UNLINKED group -> nothing stored, NOTHING SENT")
    line(f"--> event=message  chat={stranger}  body='@17473209317 who are you'")
    body = show(post(client, message_body(stranger, "@17473209317 who are you")))
    check(body == {"status": "ignored", "reason": "unknown_group"}, "200 ignored/unknown_group")
    check(households_for(stranger) == [], "still no household for the stranger's group")
    stays_silent()

    # --- 8 ---------------------------------------------------------------------------------
    step("8", "an ordinary message in the LINKED group -> ingested, NO reply")
    before = len(outbound)
    body = show(post(client, message_body(genuine, "she slept badly again last night")))
    check(body.get("status") == "ok" and body.get("inserted") == 1, "ingested")
    check(body.get("reply") == "not_mentioned", "no reply: Penny was not addressed")
    time.sleep(1.5)
    check(len(outbound) == before, "nothing sent")

    # --- 9 & 10 ----------------------------------------------------------------------------
    step("9", "five messages arrive -> NO extraction run (the cadence gate holds)")
    # Drain first. Steps 6-8 already left messages waiting, and a backlog carried in from them
    # would cross the threshold early and make step 9 fail for a reason that has nothing to do
    # with the gate. `force=True` is the import flow's escape hatch and skips the gate outright,
    # so draining here cannot itself be what proves anything below.
    drain(household_id)
    line(f"    extract_min_unextracted={holder.settings.extract_min_unextracted}")
    line(f"    unextracted backlog after draining: {unextracted_count(household_id)}")
    runs.clear()
    for index in range(5):
        post(client, message_body(genuine, f"message number {index} about the appointment"))
    wait_for(lambda: len(runs) >= 5, timeout=25.0)
    time.sleep(1.0)
    deferred = [r for r in runs if getattr(r, "deferred", False)]
    ran = [r for r in runs if getattr(r, "ran", False) and getattr(r, "messages_considered", 0)]
    line(
        f"    extraction calls: {len(runs)}   deferred: {len(deferred)}   actually ran: {len(ran)}"
    )
    check(len(ran) == 0, "NO extraction run happened")
    check(len(deferred) >= 1, "the gate deferred, with a reason")

    step("10", "one more message crosses the threshold -> exactly ONE run, covering all of them")
    pending_before = max((getattr(r, "messages_pending", 0) for r in runs), default=0)
    line(f"    unextracted backlog the gate last saw: {pending_before}")
    runs.clear()
    post(client, message_body(genuine, "and the cardiology appointment is on the 30th"))
    wait_for(lambda: any(getattr(r, "messages_considered", 0) > 0 for r in runs), timeout=30.0)
    time.sleep(1.0)
    ran = [r for r in runs if getattr(r, "messages_considered", 0) > 0]
    for summary in ran:
        line(f"    RAN: considered={summary.messages_considered} chunks={summary.chunks}")
    check(len(ran) == 1, "exactly ONE extraction run")
    if ran:
        check(
            ran[0].messages_considered >= holder.settings.extract_min_unextracted,
            f"it covered the whole batch ({ran[0].messages_considered} messages in one run)",
        )

    # --- 11 --------------------------------------------------------------------------------
    step("11", "the generated credentials")
    from app.onboarding import generate_username
    from app.seed import generate_passphrase

    for _ in range(3):
        username, passphrase = generate_username(), generate_passphrase()
        line(f"    username={username!r}  passphrase={passphrase!r}")
        check(not any(c.isdigit() for c in username), f"username {username!r} has no digits")
        check(len(passphrase.split("-")) == 4, f"passphrase {passphrase!r} is exactly four words")

    # --- 12 --------------------------------------------------------------------------------
    step("12", "the welcome message names the @-handle and keeps the three questions")
    welcome = outbound[0][1] if outbound else ""
    check("@Penny" in welcome, "the welcome names the literal @Penny handle")
    check("mention" in welcome.lower(), "the welcome explains that Penny only replies when @'d")
    for number in ("1.", "2.", "3."):
        check(number in welcome, f"onboarding question {number} is still in the welcome")
    check("password" in welcome.lower(), "the scroll-back password warning is still there")

    # --- regressions -----------------------------------------------------------------------
    step("R", "regressions")
    raw = json.dumps(message_body(genuine, "unsigned")).encode()
    response = client.post(
        "/api/whatsapp/webhook", content=raw, headers={"Content-Type": "application/json"}
    )
    check(response.status_code == 401, f"unsigned body rejected ({response.status_code})")
    response = client.post(
        "/api/whatsapp/webhook",
        content=raw,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64, "Content-Type": "application/json"},
    )
    check(response.status_code == 401, f"wrong signature rejected ({response.status_code})")

    tampered = raw.replace(b"unsigned", b"tampered")
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/whatsapp/webhook",
        content=tampered,
        headers={
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        },
    )
    check(response.status_code == 401, "a body signed over DIFFERENT bytes is rejected")

    # Built ONCE: `message_body` mints a fresh message id per call, so signing one body and
    # sending another is a 401 that says nothing about the echo filter.
    echo = message_body(genuine, "echo of Penny's own welcome", is_from_me=True)
    check(
        show(post(client, echo)).get("reason") == "from_me",
        "Penny's own echoed message is ignored (she cannot answer her own welcome)",
    )

    # /api/health is a pure function with NO session dependency, so "the DB is unreachable" is
    # not a state it can observe. Prove that structurally rather than by breaking Postgres: if
    # the route ever grows a `SessionDep`, this assertion fails and the Railway healthcheck
    # stops being able to turn a Postgres blip into a restart loop.
    import inspect

    from app.routers.health import health as health_route

    check(
        not inspect.signature(health_route).parameters,
        "/api/health takes NO dependencies — it cannot touch the DB even when Postgres is down",
    )
    health = client.get("/api/health")
    check(health.status_code == 200, f"/api/health answers 200 ({health.status_code})")

    # --- the credentials in that welcome actually work ---------------------------------------
    step("A", "the credentials Penny posted sign in, and see only their own household")
    username, passphrase = credentials_from(welcome)
    response = client.post("/api/auth/login", json={"username": username, "password": passphrase})
    check(response.status_code == 204, f"POST /api/auth/login -> {response.status_code}")
    check(client.get("/api/me").status_code == 200, "GET /api/me is authenticated")

    # Cross-household isolation: a random event id must be 404, byte-identical to another
    # household's real one. A 403 would confirm the row exists somewhere, which is the leak.
    response = client.delete(f"/api/events/{uuid4()}")
    check(response.status_code == 404, "an event of another household -> 404 (not 403)")
    client.post("/api/auth/logout")

    # --- the log audit -----------------------------------------------------------------------
    step("L", "NOTHING logged the passphrase, the message text or a prompt")
    logs = capture.text()
    line(f"    {len(logs.splitlines())} log lines captured across the whole run")
    check(passphrase not in logs, "the passphrase appears in NO log line")
    check(username not in logs, "the username appears in NO log line")
    for secret in ("she slept badly again last night", "anyone free thursday", "who are you"):
        check(secret not in logs, f"message text {secret!r} appears in NO log line")
    check("WHAT YOU KNOW" not in logs, "no prompt text was logged")
    check(SECRET not in logs, "the webhook signing secret appears in NO log line")


def credentials_from(welcome: str) -> tuple[str, str]:
    """Pull the login out of the welcome exactly as a family member reading it would."""
    username = passphrase = ""
    for text_line in welcome.splitlines():
        if text_line.startswith("Username:"):
            username = text_line.partition(":")[2].strip()
        elif text_line.startswith("Password:"):
            passphrase = text_line.partition(":")[2].strip()
    return username, passphrase


if __name__ == "__main__":
    raise SystemExit(main())
