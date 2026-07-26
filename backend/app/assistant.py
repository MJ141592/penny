"""The @-mention reply. Penny answers in the group when — and only when — she is addressed.

`answer_mention` is the whole public surface, and its contract is deliberately lopsided:
**None means say nothing, and None is the answer to every doubt.** No configuration, no key,
no budget, no events, a model that refused, a model that timed out, a database that fell over
mid-request: all of them come out here as None and the webhook posts nothing. A missed reply
is a family repeating themselves; an unwanted message is Penny talking in a chat nobody asked
her to talk in, which is the failure this whole milestone exists to make impossible.

THREE THINGS THIS FILE IS CAREFUL ABOUT

1. **The dangerous input never reaches the model.** `is_emergency` runs before any network
   call and returns fixed, server-authored words. It cannot be argued out of them by a clever
   message, it cannot hallucinate a dose, and it still works when OpenAI is down — which is
   precisely the moment silence would be worst. The model gets a second shot at the same
   judgement (`kind="emergency"`), and when it takes it the server again substitutes its own
   text: there is no path by which Penny attempts triage.

2. **The safety wording is ours, not the model's.** The standing "I'm not a clinician" line
   and the website link are appended here, keyed off `AssistantReply.kind`. A WhatsApp message
   has no page around it to carry a disclaimer, and a disclaimer the model may or may not have
   remembered to write is not a disclaimer. For the same reason every URL the model emits is
   stripped: in a group chat, "log in at ..." is an attack, and the only URL we will ever send
   is the one in our own settings.

3. **Nothing raises.** The webhook has already promised GOWA a 200; an exception escaping into
   it becomes a retry, and a retry becomes a duplicate message in a family's chat. So the whole
   IO half sits under one `except Exception`, logged by exception CLASS ONLY — never the text,
   never the prompt, never the reply (see `app/llm/gateway.py`).

The context the model is given is exactly what the database already knows: the care brief that
extraction uses, the last ~20 events, the last ~20 messages, the household timezone and today's
date. Nothing is fetched from anywhere else, because the prompt's central instruction is that
there IS nowhere else.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

# `_HouseholdBudget` is private to the extraction service by name only: it is a `BudgetGuard`
# over `llm_runs.cost_usd` for one household, and a chat reply spends from the same $15/month
# pot as everything else — which is what stops a mention loop in a busy group becoming a bill.
# Reusing it beats a second copy of the 30-day window arithmetic that could drift from the one
# the import path enforces.
from app.extraction.service import _HouseholdBudget, build_care_brief
from app.llm.gateway import CallSpec, LLMGateway, Purpose
from app.llm.recorder import DbRunRecorder
from app.models import Event, Message

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Household

log = logging.getLogger(__name__)

# `llm_runs.purpose` is a free-text column on purpose ("the gateway owns this vocabulary and
# adding a value must not need a migration"), so replies land in the cost audit under their own
# name today. The cast is here because `gateway.Purpose` is a closed Literal owned by another
# file; ask for "assistant" to be added to it and delete this line.
ASSISTANT_PURPOSE = cast(Purpose, "assistant")

# A reply is a few sentences. The ceiling exists for reasoning tokens, which bill at the output
# rate and are the only way a chat turn can get expensive; 2,000 is ~$0.06 worst case.
ASSISTANT_MAX_OUTPUT_TOKENS = 2_000
ASSISTANT_EFFORT = "low"

RECENT_EVENTS = 20
RECENT_MESSAGES = 20
# One WhatsApp message someone reads on a phone at 3am, not a document. The prompt asks for
# under 600 characters; this is the enforcement, and it is deliberately close to the ask.
MAX_REPLY_CHARS = 700
MESSAGE_EXCERPT_CHARS = 300
EVENT_BODY_CHARS = 220
# "@Penny" on its own is an address, not a question, and a one-character question is a typo.
# Both are worth a silent return rather than a paid call that can only guess.
MIN_QUESTION_CHARS = 2

ReplyKind = Literal[
    "answer",
    "not_in_record",
    "clinical_redirect",
    "about_penny",
    "acknowledgement",
    "emergency",
    "decline",
]

# Appended by US, never by the model, to the kinds where a family could otherwise read a
# sentence about their relative's health as coming from something that knows medicine.
STANDING_LINE = (
    "I keep the timeline — I'm not a clinician, so please check anything medical with your GP "
    "or pharmacist."
)

# The one thing Penny says to something urgent. No triage, no severity judgement, no questions
# back. Written to survive being wrong: the second paragraph is what a family gets when they
# were describing something that happened last month, and it is still a useful reply.
EMERGENCY_REPLY = (
    "I can't help with anything urgent, and I'm not able to judge how serious this is.\n"
    "\n"
    "If it's happening now, please call 999 (or your local emergency number), or your GP if "
    "you're not sure.\n"
    "\n"
    "If it's already been dealt with, send what happened here and I'll keep it in the timeline."
)

# Unambiguously acute phrasings only, and every one of them is a phrase rather than a word.
# `stroke`, `fall` and `fit` are all deliberately absent: "she had a stroke two years ago" is
# the single most likely sentence in the reply to Penny's own onboarding question, and turning
# a family's history into a 999 message would be its own kind of harm.
_EMERGENCY_PATTERNS = (
    r"unresponsive",
    r"unconscious",
    r"\bpassed out\b",
    r"\bcollapsed\b",
    r"(not|isn'?t|stopped|barely) breathing",
    r"(can'?t|cannot|couldn'?t|struggling to|trouble) breath",
    r"\bchest pains?\b",
    r"pain in (her|his|their|my|the) chest",
    r"(hit|banged|bumped|knocked|hurt) (her|his|their|the) head",
    r"head injury",
    r"\bseizures?\b",
    r"convuls",
    r"turning blue",
    r"(bleeding (heavily|badly)|won'?t stop bleeding)",
    r"(face|mouth) (has |is )?(dropped|drooping)",
    r"slurr(ed|ing) (her |his |their )?(speech|words)",
    r"(can'?t|cannot) (wake|rouse) (her|him|them)",
    r"(can'?t|cannot) get (her|him|them) up",
    r"took too many( of| )",
    r"overdose",
)
_EMERGENCY = re.compile("|".join(_EMERGENCY_PATTERNS), re.IGNORECASE)

# WhatsApp renders a mention as "@447700900123" or "@Penny"; neither is part of the question.
_AT_TOKEN = re.compile(r"@[\w.+-]+")
_LEADING_PENNY = re.compile(r"^\s*penny\b[\s,:;!?-]*", re.IGNORECASE)
_TRAILING_PENNY = re.compile(r"[\s,]*\bpenny\b[\s,]*([?.!]*)\s*$", re.IGNORECASE)

# Scheme-ful and www URLs only. It cannot catch a bare "evil.example", and it is not trying to:
# it exists so that the ONE link Penny ever sends is the one from our own settings, and so a
# message in the group cannot talk her into publishing a different login page.
_URL = re.compile(r"(https?://\S+|\bwww\.\S+)", re.IGNORECASE)

_DETAIL_KEYS = ("status", "provider_name", "location", "outcome", "dose_text", "action", "severity")
_UNINFORMATIVE = frozenset({None, "", "unknown", "other"})


def _load_prompt(name: str) -> tuple[str, str]:
    """Text plus a version derived from its own bytes — editing the prompt IS bumping it.

    The same two lines as `app.llm.prompts.__init__`, which is where this belongs the moment
    that file can take `ASSISTANT_PROMPT, ASSISTANT_PROMPT_VERSION = _load("assistant.md")`.
    """
    text = (Path(__file__).parent / "llm" / "prompts" / name).read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


ASSISTANT_PROMPT, ASSISTANT_PROMPT_VERSION = _load_prompt("assistant.md")


class AssistantReply(BaseModel):
    """What the model returns. `kind` FIRST, deliberately.

    Structured output is generated in field order, so the model classifies the message before
    it writes a word of the answer — and `kind` is what the server acts on: `emergency` and
    `decline` mean the prose is discarded entirely, `clinical_redirect` gets the standing line,
    `about_penny` gets the real URL. A model that writes beautifully and classifies carelessly
    is the failure mode this ordering is aimed at.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ReplyKind
    reply: str | None = Field(
        description=(
            "The exact plain-text message to send to the group, under 600 characters. "
            "No markdown, no asterisks, no links. Null when kind is 'decline'."
        )
    )


@dataclass(frozen=True, slots=True)
class MentionContext:
    """Everything the model is allowed to know, already rendered. Built once, testable alone.

    Held as strings rather than ORM rows so the prompt-shaping half of this module is a pure
    function of plain data: the four transcripts in `tests/test_assistant.py` build one of
    these by hand and never open a database.
    """

    timezone: str
    today: str
    care_brief: str
    events: tuple[str, ...]
    messages: tuple[str, ...]

    def render(self, question: str) -> str:
        timeline = "\n".join(self.events) or "(the timeline is empty)"
        conversation = "\n".join(self.messages) or "(no recent messages)"
        return (
            f'<today timezone="{self.timezone}">{self.today}</today>\n'
            f"<care_brief>\n{self.care_brief.strip()}\n</care_brief>\n"
            f'<timeline events="{len(self.events)}">\n{timeline}\n</timeline>\n'
            f"<recent_messages>\n{conversation}\n</recent_messages>\n"
            f"<question>\n{question.strip()}\n</question>"
        )


async def answer_mention(
    session: AsyncSession,
    household: Household,
    message_text: str,
    *,
    gateway: LLMGateway | None = None,
) -> str | None:
    """The reply to post in the group, or None to stay silent. Never raises.

    `gateway` is a test seam and nothing else; the real caller passes nothing and gets a gateway
    wired to this household's `llm_runs` audit rows and monthly budget.
    """
    question = strip_mention(message_text)
    if len(question) < MIN_QUESTION_CHARS:
        return None
    # BEFORE the network call, and before anything that can fail. See the module docstring.
    if is_emergency(question):
        return EMERGENCY_REPLY

    try:
        gateway = gateway or LLMGateway(
            recorder=DbRunRecorder(session, household.id),
            budget=_HouseholdBudget(session),
        )
        await gateway.check_budget(household.id, purpose=ASSISTANT_PURPOSE)
        context = await build_mention_context(session, household)
        return await compose_reply(gateway, context, question, household_id=household.id)
    except Exception as error:
        # CLASS ONLY. Not the message, not the question, not a traceback that could quote
        # either — this is a family's health conversation, and a log line is not the place
        # for it. The audit row the gateway already wrote is where a failure gets diagnosed.
        log.warning(
            "assistant.reply_failed household=%s error=%s",
            household.id,
            type(error).__name__,
        )
        return None


async def build_mention_context(session: AsyncSession, household: Household) -> MentionContext:
    """The three blocks, read from this household's rows and nowhere else."""
    tz = ZoneInfo(household.timezone)
    now = datetime.now(UTC)
    return MentionContext(
        timezone=household.timezone,
        today=now.astimezone(tz).strftime("%Y-%m-%d (%A)"),
        care_brief=await build_care_brief(session, household),
        events=await _recent_events(session, household.id, tz, now),
        messages=await _recent_messages(session, household.id, tz),
    )


async def compose_reply(
    gateway: LLMGateway,
    context: MentionContext,
    question: str,
    *,
    household_id: UUID | None = None,
) -> str | None:
    """One gateway call and the server's own post-processing. Raises; `answer_mention` catches.

    `model` is left unset, so this uses `LLM_MODEL_EXTRACT` like every other call — one model
    id in the audit trail until there is a reason for a second, and the reply is the cheapest
    call the product makes either way.
    """
    result = await gateway.structured(
        CallSpec(
            purpose=ASSISTANT_PURPOSE,
            instructions=ASSISTANT_PROMPT,
            input=context.render(question),
            schema=AssistantReply,
            max_output_tokens=ASSISTANT_MAX_OUTPUT_TOKENS,
            reasoning_effort=ASSISTANT_EFFORT,
            household_id=household_id,
            prompt_version=ASSISTANT_PROMPT_VERSION,
        )
    )
    return finish_reply(result.parsed)


def finish_reply(parsed: AssistantReply) -> str | None:
    """Turn what the model returned into what we are willing to send. Pure, so it is provable.

    The two substitutions are the point: an `emergency` reply is OUR sentence whatever the model
    wrote, and an `about_penny` reply carries the URL from settings rather than one the model
    remembered. Everything else is trimming.
    """
    if parsed.kind == "emergency":
        return EMERGENCY_REPLY
    if parsed.kind == "decline":
        return None

    body = _URL.sub("", parsed.reply or "")
    # Tidy what stripping a URL (and an enthusiastic model) leaves behind: runs of spaces,
    # trailing spaces before a newline, and more than one blank line.
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", body)).strip()
    if not body:
        # A non-decline kind with nothing in it is a broken response, not an instruction to
        # improvise. Silence is the safe reading.
        return None
    body = _truncate(body, MAX_REPLY_CHARS)

    if parsed.kind == "clinical_redirect":
        return f"{body}\n\n{STANDING_LINE}"
    if parsed.kind == "about_penny":
        return f"{body}\n\n{get_settings().app_public_url.rstrip('/')}"
    return body


def strip_mention(text: str | None) -> str:
    """The question, with the way it addressed Penny removed.

    Detection of the mention itself belongs to `app.mentions`; this only tidies the text so the
    model is not asked to interpret "@447700900123" as part of the sentence.
    """
    if not text:
        return ""
    cleaned = _AT_TOKEN.sub(" ", text)
    cleaned = _LEADING_PENNY.sub("", cleaned)
    cleaned = _TRAILING_PENNY.sub(r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def is_emergency(text: str) -> bool:
    """Does this describe something that needs a person, now? Conservative in ONE direction.

    A false positive costs a family one clumsy message telling them to ring 999 about something
    that happened in March. A false negative is a language model deciding how urgent a head
    injury is. The list is tuned accordingly.
    """
    return bool(_EMERGENCY.search(text))


# --- context reads --------------------------------------------------------------------


async def _recent_events(
    session: AsyncSession,
    household_id: UUID,
    tz: ZoneInfo,
    now: datetime,
) -> tuple[str, ...]:
    """The newest events, oldest first, one compact line each.

    Fetched newest-first and then reversed: "the last 20" has to be chosen from the top of the
    feed, but a model reading a timeline reads it forwards. Future-dated rows sort to the top
    and are labelled, which is what makes "when is her next appointment" answerable at all.
    """
    rows = (
        await session.execute(
            sa.select(
                Event.kind,
                Event.occurred_at,
                Event.occurred_at_precision,
                Event.title,
                Event.body,
                Event.details,
            )
            .where(Event.household_id == household_id, Event.deleted_at.is_(None))
            .order_by(Event.occurred_at.desc(), Event.id.desc())
            .limit(RECENT_EVENTS)
        )
    ).all()
    return tuple(
        _event_line(
            kind=row.kind,
            occurred_at=row.occurred_at,
            precision=row.occurred_at_precision,
            title=row.title,
            body=row.body,
            details=row.details or {},
            tz=tz,
            now=now,
        )
        for row in reversed(rows)
    )


async def _recent_messages(
    session: AsyncSession,
    household_id: UUID,
    tz: ZoneInfo,
) -> tuple[str, ...]:
    """The tail of the conversation, oldest first. System lines are not conversation."""
    rows = (
        await session.execute(
            sa.select(
                Message.sent_at,
                Message.sender_display_name,
                Message.text,
                Message.message_type,
            )
            .where(Message.household_id == household_id, Message.message_type != "system")
            .order_by(Message.sent_at.desc(), Message.source_ordinal.desc(), Message.id.desc())
            .limit(RECENT_MESSAGES)
        )
    ).all()
    return tuple(
        _message_line(
            sent_at=row.sent_at,
            sender=row.sender_display_name,
            text=row.text,
            message_type=row.message_type,
            tz=tz,
        )
        for row in reversed(rows)
    )


def _event_line(
    *,
    kind: str,
    occurred_at: datetime,
    precision: str,
    title: str,
    body: str | None,
    details: dict[str, object],
    tz: ZoneInfo,
    now: datetime,
) -> str:
    line = f"{_event_date(occurred_at, precision, tz)} {kind}: {title.strip()}"
    if occurred_at > now:
        line += " (upcoming)"
    extras = ", ".join(
        f"{key}: {details[key]}"
        for key in _DETAIL_KEYS
        if isinstance(details.get(key), str) and details[key] not in _UNINFORMATIVE
    )
    if extras:
        line += f" [{extras}]"
    if body:
        line += f" — {_truncate(' '.join(body.split()), EVENT_BODY_CHARS)}"
    return line


def _event_date(occurred_at: datetime, precision: str, tz: ZoneInfo) -> str:
    """Print the date to the precision we actually have, so the model cannot borrow certainty.

    `events.occurred_at` is NOT NULL and falls back to a source message's timestamp, so an
    undated event still carries a full timestamp that means nothing. Rendering `week`, `month`
    and `unknown` as themselves is what stops "sometime in August" being answered as the 3rd.
    """
    local = occurred_at.astimezone(tz)
    if precision == "exact":
        return local.strftime("%Y-%m-%d %H:%M")
    if precision == "week":
        return f"week of {local:%Y-%m-%d}"
    if precision == "month":
        return f"{local:%Y-%m} (month only)"
    if precision == "unknown":
        return f"{local:%Y-%m-%d} (date uncertain)"
    return local.strftime("%Y-%m-%d")


def _message_line(
    *,
    sent_at: datetime,
    sender: str | None,
    text: str | None,
    message_type: str,
    tz: ZoneInfo,
) -> str:
    who = (sender or "someone").strip()
    said = " ".join((text or "").split())
    if not said:
        # Media is stored as a type and nothing else (auto-download is off), so this is the
        # whole truth about the message rather than a summary of something we dropped.
        said = f"({message_type}, no text)"
    return (
        f"{sent_at.astimezone(tz):%Y-%m-%d %H:%M} {who}: {_truncate(said, MESSAGE_EXCERPT_CHARS)}"
    )


def _truncate(text: str, limit: int) -> str:
    """Cut at a word boundary and say so. A silently halved sentence reads as a bug."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    head, _, _tail = cut.rpartition(" ")
    # Back off to the last word boundary, unless doing so would throw away half the excerpt —
    # one very long token should still be cut mid-token rather than reduced to nothing.
    if head and len(head) >= limit // 2:
        cut = head
    return cut.rstrip(" ,;:") + "…"
