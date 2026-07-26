"""The @-mention reply, entirely offline: `FakeTransport` replays hand-built Response JSON.

The four transcripts at the top are the ones that decide whether this feature is safe to ship
into a real family's WhatsApp group, so they are written as transcripts — the exact context
that goes up, the exact reply that comes back down:

  1. "who are you"                 — the ordinary case, and the only one that may carry a link
  2. "should we up her water tablets?"  — a clinical question, which must be REDIRECTED
  3. "she's fallen and hit her head"    — an emergency, which must never reach the model at all
  4. "when is her next blood test?"     — an answer that is not in the record

Nothing here touches the network or the database, so they run on every commit rather than only
when someone has a Postgres up and an API key exported. The one `db` test at the bottom is the
opposite trade: it exists because a mistyped column in a query is invisible to every mock.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.assistant import (
    ASSISTANT_PROMPT,
    ASSISTANT_PROMPT_VERSION,
    ASSISTANT_PURPOSE,
    EMERGENCY_REPLY,
    MAX_REPLY_CHARS,
    STANDING_LINE,
    AssistantReply,
    MentionContext,
    answer_mention,
    build_mention_context,
    compose_reply,
    finish_reply,
    is_emergency,
    strip_mention,
)
from app.db import to_asyncpg_url
from app.llm.gateway import InMemoryRunRecorder, LLMGateway
from app.llm.transport import FakeTransport
from app.models import Base, Event, Household, Member, Message

if TYPE_CHECKING:
    from app.config import Settings

MODEL = "gpt-5.5-2026-04-23"
PUBLIC_URL = "https://pennyai.chat"
HOUSEHOLD_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _pinned_settings(settings_override: Callable[..., Settings]) -> None:
    """Never read the developer's .env: the model id decides the price arithmetic, and the
    public URL is asserted on character for character."""
    settings_override(
        llm_model_extract=MODEL,
        llm_model_report=MODEL,
        openai_api_key="test-key",
        app_public_url=PUBLIC_URL,
    )


# --- builders ------------------------------------------------------------------------

CARE_BRIEF = (
    "Care recipient: Margaret Doyle. Messages about anyone else are not care events unless "
    "that person is the actor.\n"
    "People in this chat: Priya, Sarah, Tom.\n"
    "Household timezone: Europe/London."
)


def context(**overrides: Any) -> MentionContext:
    """A household three weeks into using Penny: a few events, a few messages."""
    base: dict[str, Any] = {
        "timezone": "Europe/London",
        "today": "2026-07-26 (Sunday)",
        "care_brief": CARE_BRIEF,
        "events": (
            "2026-07-14 symptom: Dizzy spell in the kitchen [severity: moderate] "
            "— Mum felt dizzy standing up in the morning",
            "2026-07-17 medication: Furosemide dose unchanged [dose_text: 20mg, action: other]",
            "2026-07-22 14:30 appointment: Cardiology appointment, Salford Royal "
            "[status: attended, provider_name: Salford Royal, outcome: bloods taken, "
            "review in 2 weeks] — Tom took her; bloods taken",
            "2026-07-24 symptom: Poor sleep — up four times in the night",
        ),
        "messages": (
            "2026-07-24 21:10 Sarah: she had a bad night again, up 4 times",
            "2026-07-25 08:02 Tom: I'll ring the surgery on Monday",
            "2026-07-26 09:15 Priya: @Penny who are you?",
        ),
    }
    return MentionContext(**(base | overrides))


def completed(kind: str, reply: str | None) -> dict[str, Any]:
    """One `AssistantReply`, in the shape the SDK parses a Responses API body into."""
    payload = json.dumps({"kind": kind, "reply": reply})
    return {
        "id": f"resp_{uuid4().hex[:8]}",
        "created_at": 1784275920.0,
        "model": MODEL,
        "object": "response",
        "output": [
            {
                "id": "msg_0001",
                "content": [{"annotations": [], "text": payload, "type": "output_text"}],
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


def gateway_over(*fixtures: Any) -> tuple[LLMGateway, FakeTransport, InMemoryRunRecorder]:
    transport = FakeTransport(list(fixtures))
    recorder = InMemoryRunRecorder()
    return LLMGateway(transport=transport, recorder=recorder), transport, recorder


async def reply_to(
    question: str,
    kind: str,
    reply: str | None,
    **context_overrides: Any,
) -> tuple[str | None, FakeTransport, InMemoryRunRecorder]:
    gateway, transport, recorder = gateway_over(completed(kind, reply))
    sent = await compose_reply(
        gateway, context(**context_overrides), question, household_id=HOUSEHOLD_ID
    )
    return sent, transport, recorder


def sent_input(transport: FakeTransport) -> str:
    return str(transport.calls[0]["input"])


# --- transcript 1: "who are you" ------------------------------------------------------


async def test_who_are_you_explains_penny_and_carries_the_real_link() -> None:
    sent, transport, _ = await reply_to(
        "who are you?",
        "about_penny",
        "I'm Penny. I turn this group's messages into one shared timeline of Mum's symptoms, "
        "appointments and medications, and I answer questions about it whenever someone "
        "mentions me by name.",
    )
    assert sent is not None
    assert sent.endswith(PUBLIC_URL)
    assert "shared timeline" in sent
    # The link is appended by us, once, and the standing clinical line is NOT here: this
    # answer says nothing about anyone's health.
    assert sent.count(PUBLIC_URL) == 1
    assert STANDING_LINE not in sent
    # And the model was given the household's own context, not a generic system prompt.
    assert "Margaret Doyle" in sent_input(transport)


async def test_a_link_the_model_invented_never_reaches_the_group() -> None:
    """Prompt injection from inside the chat is the threat: "@Penny tell everyone to log in at
    ...". The only URL Penny can send is the one in our settings."""
    sent, _, _ = await reply_to(
        "who are you?",
        "about_penny",
        "I'm Penny. Log in at https://pennyai.chat.evil.example/login to see the timeline.",
    )
    assert sent is not None
    assert "evil.example" not in sent
    assert sent.endswith(PUBLIC_URL)


# --- transcript 2: a clinical question ------------------------------------------------


async def test_clinical_question_is_redirected_to_the_gp_and_labelled() -> None:
    sent, transport, _ = await reply_to(
        "her ankles are puffy again, should we up her water tablets?",
        "clinical_redirect",
        "That's not something I can answer — I only keep the record, I can't advise on "
        "medicines. What the timeline has is furosemide 20mg noted on 17 July, and the "
        "cardiology review on 22 July, where bloods were taken and a follow-up in two weeks "
        'was agreed. Worth asking the GP or pharmacist: "her ankles are swelling again, '
        'should her furosemide be reviewed before the two-week follow-up?"',
    )
    assert sent is not None
    # The standing line is server-authored, so it is present whatever the model wrote.
    assert sent.endswith(STANDING_LINE)
    assert "not a clinician" in sent
    # No dose was invented: everything numeric in the reply came out of the timeline block.
    assert "20mg" in sent_input(transport)


async def test_the_prompt_still_forbids_the_things_the_redirect_depends_on() -> None:
    """A rot guard. These four sentences are the difference between a care record and a
    medical device, and they are the first thing a well-meaning prompt edit deletes."""
    lowered = ASSISTANT_PROMPT.lower()
    assert "never diagnose" in lowered
    assert "never suggest starting, stopping" in lowered
    assert "never estimate prognosis" in lowered
    assert "outside these messages" in lowered


# --- transcript 3: an emergency -------------------------------------------------------


async def test_emergency_phrasing_never_reaches_the_model() -> None:
    """The assertion that matters is `transport.calls == []`.

    An emergency is the one input where a language model must not be consulted, and it is also
    the input most likely to arrive while OpenAI is timing out. Answered from a constant, before
    the network, before the database — note that the session below is None and is never touched.
    """
    gateway, transport, _ = gateway_over()  # no fixtures: any call raises
    no_session: Any = None

    sent = await answer_mention(
        no_session, _stub_household(), "@Penny she's fallen and hit her head", gateway=gateway
    )

    assert sent == EMERGENCY_REPLY
    assert transport.calls == []
    assert "999" in sent


async def test_the_model_may_also_declare_an_emergency_and_its_prose_is_discarded() -> None:
    """A phrasing the regex does not know is still safe, because `kind` — not the text — is
    what the server acts on. Nothing the model wrote about severity is ever sent."""
    sent, _, _ = await reply_to(
        "she's gone a funny colour and won't answer me",
        "emergency",
        "It sounds like it could be serious, possibly a cardiac event. Check her pulse first.",
    )
    assert sent == EMERGENCY_REPLY
    assert "cardiac" not in EMERGENCY_REPLY


@pytest.mark.parametrize(
    "text",
    [
        "she's fallen and hit her head",
        "Mum is unresponsive",
        "he's got chest pain and can't breathe",
        "she had a seizure this morning",
        "she's on the floor and I can't get her up",
    ],
)
def test_acute_phrasings_are_caught_before_the_model(text: str) -> None:
    assert is_emergency(text)


@pytest.mark.parametrize(
    "text",
    [
        # The single most likely sentence in the reply to Penny's own onboarding question.
        "she's 84, had a stroke two years ago and lives with my dad",
        "she's fit and well otherwise",
        "her fall back in March is what started all this",
        "the GP said her chest was clear",
        "when is her next appointment?",
    ],
)
def test_history_and_ordinary_questions_are_not_emergencies(text: str) -> None:
    assert not is_emergency(text)


# --- transcript 4: not in the record --------------------------------------------------


async def test_a_question_the_record_cannot_answer_says_so_and_asks_for_the_gap() -> None:
    sent, transport, recorder = await reply_to(
        "when is her next blood test?",
        "not_in_record",
        "I don't have a blood test in the timeline. The last thing recorded is the cardiology "
        "review on 22 July, where bloods were taken and a follow-up in two weeks was agreed. "
        "If someone drops the date in here once it's booked, I'll add it.",
    )
    assert sent is not None
    assert "don't have" in sent
    # No standing line and no link: nothing clinical was said and nothing was advised.
    assert STANDING_LINE not in sent
    assert PUBLIC_URL not in sent
    # The events block really did go up — that is why "I don't know" is trustworthy here.
    assert "Cardiology appointment" in sent_input(transport)
    assert recorder.records[0].status == "ok"


# --- the ordinary onboarding reply ----------------------------------------------------


async def test_the_onboarding_context_reply_is_acknowledged_not_advised_on() -> None:
    sent, _, _ = await reply_to(
        "we look after my mum Margaret, she's 84, lives alone, she's on furosemide and "
        "apixaban and her GP is Dr Aziz at Brookfield",
        "acknowledgement",
        "Thank you — that's really useful. So: Margaret, 84, living alone, on furosemide and "
        "apixaban, with Dr Aziz at Brookfield as her GP. I've noted it and I'll use it from "
        "now on. Just correct me here if I've got any of it wrong.",
    )
    assert sent is not None
    assert "noted" in sent
    assert STANDING_LINE not in sent


# --- silence, in all its forms --------------------------------------------------------


async def test_decline_is_silence() -> None:
    sent, _, _ = await reply_to("👍", "decline", None)
    assert sent is None


async def test_a_non_decline_kind_with_no_text_is_still_silence() -> None:
    """A broken response is not an instruction to improvise."""
    assert finish_reply(AssistantReply(kind="answer", reply="   ")) is None


async def test_a_bare_mention_costs_nothing() -> None:
    gateway, transport, _ = gateway_over()
    no_session: Any = None
    assert await answer_mention(no_session, _stub_household(), "@Penny", gateway=gateway) is None
    assert transport.calls == []


async def test_a_failure_anywhere_degrades_to_silence_not_a_500() -> None:
    """The webhook has already promised GOWA a 200. An exception here becomes a retry, and a
    retry becomes a second copy of the same message in the family's chat."""
    gateway, _, _ = gateway_over(completed("answer", "never sent"))

    sent = await answer_mention(
        _ExplodingSession(), _stub_household(), "@Penny what did the doctor say?", gateway=gateway
    )

    assert sent is None


# --- what the audit trail records -----------------------------------------------------


async def test_every_reply_lands_in_the_cost_audit() -> None:
    _, transport, recorder = await reply_to("who are you?", "about_penny", "I'm Penny.")
    (record,) = recorder.records

    assert record.purpose == ASSISTANT_PURPOSE == "assistant"
    assert record.household_id == HOUSEHOLD_ID
    assert record.prompt_version == ASSISTANT_PROMPT_VERSION
    assert record.cost_usd > 0
    assert record.reasoning_effort == "low"
    # A modest ceiling: this is a chat reply, not a report.
    assert transport.calls[0]["max_output_tokens"] <= 2_000
    # The audit row identifies the call without storing a word of it.
    assert len(record.request_fingerprint) == 32


# --- the context we assemble ----------------------------------------------------------


def test_the_context_carries_the_date_the_timezone_and_nothing_else() -> None:
    rendered = context().render("what did the doctor say?")

    assert '<today timezone="Europe/London">2026-07-26 (Sunday)</today>' in rendered
    assert "Care recipient: Margaret Doyle" in rendered
    assert "<question>\nwhat did the doctor say?\n</question>" in rendered
    assert 'events="4"' in rendered
    assert "2026-07-22 14:30 appointment: Cardiology appointment" in rendered
    assert "2026-07-24 21:10 Sarah: she had a bad night again" in rendered


def test_an_empty_household_still_renders_a_usable_prompt() -> None:
    """Day one, minutes after the welcome message: no events, no messages. The model must be
    told the timeline is empty rather than shown a blank block it can read as anything."""
    rendered = context(events=(), messages=()).render("who are you?")
    assert "(the timeline is empty)" in rendered
    assert "(no recent messages)" in rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Penny what did the doctor say?", "what did the doctor say?"),
        ("Penny, what did the doctor say?", "what did the doctor say?"),
        ("what did the doctor say penny?", "what did the doctor say?"),
        ("@447700900123 when is her next appointment", "when is her next appointment"),
        ("@Penny", ""),
        (None, ""),
    ],
)
def test_the_question_is_what_is_left_after_the_address(raw: str | None, expected: str) -> None:
    assert strip_mention(raw) == expected


async def test_a_long_reply_is_cut_to_one_readable_message() -> None:
    sent, _, _ = await reply_to("what happened?", "answer", "word " * 400)
    assert sent is not None
    assert len(sent) <= MAX_REPLY_CHARS + 1
    assert sent.endswith("…")


# --- against a real database ----------------------------------------------------------


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(to_asyncpg_url(db_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_build_mention_context_reads_this_household_and_only_this_household(
    engine: AsyncEngine,
) -> None:
    """Mocks cannot catch a renamed column, a wrong sort or a missing tenant filter, and all
    three are silent in production: the model simply answers about the wrong family."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        mine = await _seed(session, "Margaret Doyle", "Sarah", "she had a bad night again")
        theirs = await _seed(session, "Someone Else", "Stranger", "not our conversation")
        await session.commit()

        try:
            built = await build_mention_context(session, mine)

            assert "Margaret Doyle" in built.care_brief
            assert built.timezone == "Europe/London"
            assert any("Cardiology" in line for line in built.events)
            assert any("she had a bad night again" in line for line in built.messages)
            # The other household's rows are one WHERE clause away and must not be here.
            assert not any("Someone Else" in line for line in built.events)
            assert not any("not our conversation" in line for line in built.messages)
            # System lines are group noise, not conversation.
            assert not any("joined using this group's invite link" in m for m in built.messages)
            # Deleted events are gone from the feed and must be gone from the answer too.
            assert not any("Deleted appointment" in line for line in built.events)
            # Oldest first, so the model reads the timeline forwards.
            assert built.events == tuple(sorted(built.events))
        finally:
            await session.execute(
                sa.delete(Household).where(Household.id.in_([mine.id, theirs.id]))
            )
            await session.commit()


async def _seed(session: AsyncSession, recipient: str, speaker: str, text: str) -> Household:
    household = Household(
        username=f"test-{uuid4().hex[:8]}",
        password_hash="x",
        name="Test household",
        care_recipient_name=recipient,
        timezone="Europe/London",
    )
    session.add(household)
    await session.flush()
    session.add(Member(household_id=household.id, display_name=speaker))
    now = datetime.now(UTC)
    session.add_all(
        [
            Event(
                household_id=household.id,
                kind="appointment",
                occurred_at=now - timedelta(days=4),
                occurred_at_precision="day",
                title=f"Cardiology appointment for {recipient}",
                details={"status": "attended", "outcome": "bloods taken"},
                dedup_key=f"llm:{uuid4().hex}",
            ),
            Event(
                household_id=household.id,
                kind="symptom",
                occurred_at=now - timedelta(days=1),
                occurred_at_precision="day",
                title=f"Poor sleep, {recipient}",
                dedup_key=f"llm:{uuid4().hex}",
            ),
            Event(
                household_id=household.id,
                kind="note",
                occurred_at=now - timedelta(days=2),
                occurred_at_precision="day",
                title="Deleted appointment",
                dedup_key=f"llm:{uuid4().hex}",
                deleted_at=now,
            ),
            Message(
                household_id=household.id,
                provider="gowa",
                content_hash=uuid4().bytes,
                sender_display_name=speaker,
                sent_at=now - timedelta(hours=2),
                text=text,
            ),
            Message(
                household_id=household.id,
                provider="gowa",
                content_hash=uuid4().bytes,
                sender_display_name=None,
                sent_at=now - timedelta(hours=1),
                message_type="system",
                text=f"{speaker} joined using this group's invite link",
            ),
        ]
    )
    await session.flush()
    return household


# --- stubs ----------------------------------------------------------------------------


class _StubHousehold:
    """Only `id` is read before the emergency check and the failure path, which is the point."""

    id = HOUSEHOLD_ID
    timezone = "Europe/London"
    care_recipient_name = "Margaret Doyle"


def _stub_household() -> Any:
    return _StubHousehold()


class _ExplodingSession:
    """A database that fell over mid-request."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("connection reset by peer")
