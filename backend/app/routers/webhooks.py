"""`POST /api/whatsapp/webhook` — the live ingestion door.

**The order of operations in `gowa_webhook` is load-bearing.** Reading it top to bottom:

0. **A cap, then raw bytes, then HMAC, then parse.** This door is unauthenticated until the
   HMAC is checked, so the bytes are counted as they arrive and the read is abandoned the
   moment the running total passes `MAX_BODY_BYTES` — see `read_capped_body`. Buffering the
   whole body first and measuring it afterwards is the same bug with a nicer error message.

1. **Raw bytes, then HMAC, then parse.** The signature covers the exact bytes GOWA sent. Parsing
   first and re-serialising the object to hash it compares a body we invented against a
   signature for a body we were given — whitespace, key order and unicode escaping all differ,
   so it would fail on valid requests and, worse, any normalisation that made it pass would let
   an attacker vary the parts that normalised away. Parsing also has to come *second* because
   parsing is work, and doing attacker-controlled work before authenticating it is the cheap
   half of a denial-of-service.

2. **Answer 200 fast, always.** GOWA retries 5x with exponential backoff on any non-2xx and
   gives up after that. So every outcome that a retry cannot fix — an unlinked group, a direct
   chat, a body we cannot parse — is a **200 with a reason**, not a 4xx. A 401 for a bad
   signature is the one exception, and that one *should* be loud. LLM extraction is handed to
   `BackgroundTasks`, which runs after the response is written; doing it inline would blow the
   10-second budget on the first busy chunk and turn one message into five deliveries.

3. **A MESSAGE NEVER PROVISIONS ANYTHING. A GENUINE JOIN IS THE ONLY TRIGGER.** This is the
   rule that was learned the expensive way: the paired WhatsApp number is a REAL account that
   was already sitting in several unrelated group chats, and provisioning on any message from
   any unknown group posted a login password into strangers' conversations. Two accidental
   households, one of them someone else's group. So the message path now provisions NOTHING —
   an unlinked group is a logged 200 and no rows — and `_handle_group_event` is the only door
   to `provision_for_group`, behind two independent gates. See its docstring.

   **When in doubt, stay silent.** A missed welcome is a support conversation. A password in a
   stranger's group chat cannot be unsent.

4. **Penny speaks only when spoken to.** A reply goes out for an explicit @-mention in a LINKED
   group and for nothing else. In an unlinked group a mention is ignored exactly like any other
   message — the mention is not a second onboarding trigger, because that is the same bug in a
   new hat. Replies are rate-limited per household: every one is an LLM call and an outbound
   WhatsApp message, and outbound volume is what gets the paired number banned.

5. **Idempotency is not implemented here.** It lives in the partial unique index on
   `(household_id, provider_message_id)` and is enforced by `ingest_messages`. This handler's
   only job is to pass `payload.id` through faithfully, because a replay is the *expected* case
   whenever we are slow, not an anomaly.

Everything about the payload shape that is verified rather than assumed is in
`docs/railway-deployment.md#webhook-contract` and `docs/gowa-runbook.md`. The two that bite:
there is **no `is_group` field** (the `@g.us` suffix is the only signal), and messages we send
are delivered back to us with `is_from_me: true` and **no way to filter them on the GOWA side**.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import gowa
from app.assistant import answer_mention
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import SessionDep
from app.errors import PennyError, UnauthorizedError
from app.extraction.service import run_extraction_for_household
from app.groups import (
    groups_first_seen_within,
    in_startup_quiet_period,
    join_burst_window_seconds,
    observe_group,
)
from app.ingest.contract import (
    GROUP_JID_SUFFIX,
    InboundMessage,
    UnknownGroupError,
)
from app.ingest.seam import ingest_messages
from app.mentions import has_mention_marker, mentions_penny, normalise_jid
from app.models import Household, Message
from app.onboarding import provision_for_group, welcome_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="
# GOWA ships with WHATSAPP_WEBHOOK_SECRET defaulting to the literal string "secret". A secret
# published in a public repo's README is not a secret; accepting it would mean anyone who can
# reach the container can inject messages into a family's health record.
FORBIDDEN_SECRETS = frozenset({"", "secret", "changeme"})

# The most body we will hold in memory for a caller who has not authenticated yet. A GOWA
# message event is a small JSON object — the largest one observed live is under 4 KB, and the
# ceiling is a long text message plus metadata — so 256 KB is roughly sixty times the real
# worst case and still small enough that two gunicorn workers cannot be pushed over by it.
MAX_BODY_BYTES = 256 * 1024
CONTENT_LENGTH_HEADER = "content-length"


class PayloadTooLargeError(PennyError):
    """413 — the body passed `MAX_BODY_BYTES` and the rest of it was never read.

    A `PennyError` subclass so it renders through the one error envelope, and 413 rather than a
    200 with a reason because it is the one refusal that a retry *can* fix: GOWA re-sending the
    same oversized body is harmless, and if a body this large is ever legitimate we want the
    retries in the log rather than a silent drop.
    """

    status_code = 413
    detail = "That request body was too large."


# Timestamps above this are milliseconds, not seconds. 1e11 seconds is the year 5138 and 1e11
# milliseconds is 1973, so the boundary is unambiguous for anything WhatsApp will ever send.
_MS_THRESHOLD = 1e11

# Media keys we may see on a message event. v1 records the TYPE and stores no text and no file:
# WHATSAPP_AUTO_DOWNLOAD_MEDIA is off, and the feed renders "photo (not stored)".
MEDIA_FIELDS: dict[str, str] = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "voice": "audio",
    "ptt": "audio",
    "document": "document",
    "sticker": "sticker",
    "contact": "contact",
    "location": "location",
    "live_location": "location",
}

# Edits, deletions and reactions are dropped AT THE ADAPTER — `app.ingest.contract` documents
# that as a deliberate v1 omission, and `messages` is append-only because of it. The exact
# shape GOWA uses for these is UNVERIFIED (the M1b spike only captured a plain text message),
# so this is a deliberately broad net: a false positive drops one message, a false negative
# writes a duplicate of an already-stored message with edited text, which is worse.
_MUTATION_KEYS = frozenset({"reaction", "edited_message", "edited_text", "revoked_message"})
_MUTATION_TYPES = frozenset(
    {"reaction", "edit", "edited", "revoke", "revoked", "delete", "deleted", "protocol"}
)

# Bounded, in-process, deliberately not a table. See `record_unlinked_group`.
_UNLINKED_LIMIT = 20
_unlinked_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()

# How long a spawned extraction waits for the request that scheduled it to commit.
# Milliseconds in practice; the ceiling only matters when the request never commits at all.
COMMIT_WAIT_TRIES = 60
COMMIT_WAIT_SECONDS = 0.25

# Strong references to detached extraction tasks. See `spawn_extraction`.
_RUNNING: set[asyncio.Task[None]] = set()

# --- @-mention replies ---------------------------------------------------------------------

# A FEW PER HOUR, PER HOUSEHOLD, AND THAT IS THE POINT. Each reply is one OpenAI call and one
# outbound WhatsApp message from a real, unappealable, single paired account — and outbound
# volume is the thing that gets such an account banned. The whole plan rests on Penny's outbound
# rate being near zero, so this is deliberately far below what a chatty family would generate.
# Exceeding it is not an error the family should see: Penny simply says nothing, which is the
# same thing she does for any message that is not addressed to her.
MAX_REPLIES_PER_HOUSEHOLD = 4
REPLY_WINDOW_SECONDS = 3600.0
# Enough households to cover every real one many times over; the bound only exists so a forged
# stream of chat ids cannot grow this dict without limit. Oldest key is evicted first.
_REPLY_LIMIT_MAX_KEYS = 512
_reply_hits: OrderedDict[UUID, deque[float]] = OrderedDict()

# The paired account's own JID, so a mention can be recognised. It arrives free on every webhook
# envelope as `device_id` (the runbook's captured body confirms the shape:
# "628123456789@s.whatsapp.net"), and `gowa.list_devices()` is the fallback for the case where a
# future GOWA drops the field. Cached because the alternative is a sidecar round trip on every
# message, and it changes only when the number is re-paired.
OWN_JID_TTL_SECONDS = 900.0
# A failed lookup is cached too, briefly. Without it, a GOWA outage means every @-containing
# message pays the timeout below — which is how a sidecar being down turns into webhook retries.
OWN_JID_FAILURE_TTL_SECONDS = 60.0
# The handler's whole budget is 10s before GOWA retries and the family gets duplicates. This is
# the most of it a mention lookup may spend, against `gowa.STATUS_TIMEOUT` of 5s.
OWN_JID_TIMEOUT_SECONDS = 2.5
_own_jid: str | None = None
_own_jid_expires_at: float = 0.0
_own_jid_lock = asyncio.Lock()

# Where the paired number might be found on a `/devices` entry. v9 answers `/app/status` with
# `jid` and `/devices` with `id`; which of them carries the phone JID on a given build is not
# something this code should have to know, so it takes the first field that looks like a number.
_DEVICE_JID_FIELDS = ("jid", "device", "phone", "device_id", "id")


class GowaMessagePayload(BaseModel):
    """`event.payload`.

    Only `id`, `timestamp`, `is_from_me`, `chat_id` and `from` are guaranteed by the contract;
    everything else is conditional, and `from_name` is **omitted entirely** when empty rather
    than sent as `""`. So: three required fields, and every other field optional with a default.

    `from` is nominally guaranteed but is modelled optional anyway. A required field turns an
    unexpected body into a 422, and a 422 is a non-2xx, and a non-2xx is five retries of a body
    that will never parse. Being generous here costs nothing; being strict costs an ingest
    outage the next time GOWA changes a field name.

    `extra="allow"` because `payload` is stored verbatim for re-extraction — but note that the
    row is written from the raw JSON dict, not from this model, so nothing this model failed to
    anticipate is lost on the way in.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    chat_id: str
    timestamp: Any = None
    is_from_me: bool = False
    from_: str | None = Field(default=None, alias="from")
    from_lid: str | None = None
    from_name: str | None = None
    body: str | None = None
    text: str | None = None
    type: str | None = None
    # POLYMORPHIC: a bare `str` when the image has no caption, a `dict` when it has one. A
    # `dict`-only annotation rejects every uncaptioned photo, which is most of them.
    image: str | dict[str, Any] | None = None
    video: str | dict[str, Any] | None = None
    audio: str | dict[str, Any] | None = None
    document: str | dict[str, Any] | None = None
    sticker: str | dict[str, Any] | None = None
    action: str | None = None


class GowaWebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str | None = None
    device_id: str | None = None
    session_id: str | None = None
    payload: GowaMessagePayload | None = None


class GowaEventHead(BaseModel):
    """Just the event name, validating nothing else, so routing never depends on the body shape.

    Every field optional and `extra="allow"`: this model must parse ANY authentic body, because
    failing here discards an event that a differently-shaped model would have handled fine.
    """

    model_config = ConfigDict(extra="allow")

    event: str | None = None


# Being ADDED to a group is not a message, so a `message`-only subscription hears nothing and
# onboarding never fires. Both events are still SUBSCRIBED — every group id they carry is fed to
# the ledger — but only `group.joined` can provision. `group.participants` fires for every
# membership change in every group Penny already sits in, so it is a poor "we are new here"
# signal and an excellent way to mint a household when a cousin joins a months-old chat.
GROUP_JOINED_EVENT = "group.joined"
GROUP_PARTICIPANTS_EVENT = "group.participants"
GROUP_EVENTS = frozenset({GROUP_JOINED_EVENT, GROUP_PARTICIPANTS_EVENT})


class GowaGroupPayload(BaseModel):
    """`group.joined` / `group.participants`.

    Shape read from the emitting source (`src/infrastructure/whatsapp/event_group.go`) rather
    than the docs, which tabulate `group.joined` but publish no example for it. Both events
    build the same map: `chat_id`, `type`, `jids`, plus an optional `group_name` and `reason`
    on the joined path. Every field but `chat_id` is optional here for the same reason the
    message payload is generous — a required field turns a shape change into a 422, and a
    non-2xx is five GOWA retries of a body that will never parse.
    """

    model_config = ConfigDict(extra="allow")

    chat_id: str
    type: str | None = None
    jids: list[str] = Field(default_factory=list)
    group_name: str | None = None
    reason: str | None = None


class GowaGroupEnvelope(BaseModel):
    """Parsed only for the group events, so the message path keeps its own strict shape."""

    model_config = ConfigDict(extra="allow")

    event: str | None = None
    device_id: str | None = None
    payload: GowaGroupPayload | None = None


async def read_capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The raw body, or `PayloadTooLargeError` before `limit` bytes are ever held.

    `await request.body()` allocates whatever the caller decides to send and only then hands it
    over to be authenticated, which makes an unauthenticated stranger's `Content-Length` the
    memory budget of a worker. There are two of them. So the body is consumed a chunk at a time
    and the read is abandoned mid-stream: the offending chunk is counted but never appended, so
    the buffer never exceeds `limit` and the rest of the upload is simply never read.

    What is returned is the exact bytes that were counted, unnormalised and un-re-serialised,
    because these are the bytes the HMAC covers. Anything that rebuilt the body here — even
    `b"".join` of a re-parsed structure — would be verifying a body we invented.

    `Content-Length` is checked first purely to fail before opening the stream at all, and it is
    a HINT: it is absent on a chunked upload and a lie whenever the sender wants it to be. It can
    only cause an early rejection, never an acceptance; the running total is what actually
    enforces the cap.
    """
    declared = _declared_length(request)
    if declared is not None and declared > limit:
        log.warning(
            "webhook.body_too_large",
            extra={"stage": "content_length", "declared": declared, "limit": limit},
        )
        raise PayloadTooLargeError

    body = bytearray()
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            # Note the ordering: counted, refused, and only the chunks BELOW the cap were ever
            # kept. `body.extend(chunk)` before this check would buffer one transport read past
            # the limit, and moving the check after the loop is the bug being fixed.
            log.warning(
                "webhook.body_too_large",
                extra={"stage": "stream", "read_bytes": total, "limit": limit},
            )
            raise PayloadTooLargeError
        body.extend(chunk)
    return bytes(body)


def _declared_length(request: Request) -> int | None:
    """`Content-Length` as an int, or None when it is absent or not a number."""
    raw = request.headers.get(CONTENT_LENGTH_HEADER)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        # An unparsable header is evidence of nothing. The counter above still decides.
        return None


def verify_signature(raw_body: bytes, header: str | None) -> None:
    """HMAC-SHA256 over the RAW BODY BYTES, constant-time. Raises 401; never returns a bool.

    Returning a bool invites `if not verify(...)` to be written as `if verify(...)` exactly once,
    in a diff nobody looks at twice, and the failure mode is an open webhook that behaves
    perfectly in every test. Raising leaves no way to accidentally ignore the result.
    """
    secret = get_settings_secret()
    if secret is None:
        # Loud and refusing, rather than quietly accepting everything. The retries this causes
        # are the point: a silent total-ingest outage is worse than a noisy one.
        log.error("webhook.secret_misconfigured")
        raise UnauthorizedError("Invalid signature.")
    if not header or not header.startswith(SIGNATURE_PREFIX):
        log.warning("webhook.signature_missing")
        raise UnauthorizedError("Invalid signature.")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header[len(SIGNATURE_PREFIX) :].strip().lower()
    if not hmac.compare_digest(expected, provided):
        log.warning("webhook.signature_invalid", extra={"byte_count": len(raw_body)})
        raise UnauthorizedError("Invalid signature.")


def get_settings_secret() -> str | None:
    """The configured webhook secret, or None if it is unset or is GOWA's published default."""
    secret = (get_settings().whatsapp_webhook_secret or "").strip()
    return None if secret.lower() in FORBIDDEN_SECRETS else secret


def record_unlinked_group(chat_id: str) -> None:
    """Remember a group that messaged us but belongs to no household.

    Deliberately in memory and deliberately bounded. It exists so the family can *see* the
    `chat_id` they need to paste into `POST /api/whatsapp/link` — before this, the only place
    that id appeared was a Railway log line, which makes linking an operator task instead of a
    self-serve one. It is not a table because it is a hint, not a record: it survives until the
    next deploy, and one more message from the group repopulates it.
    """
    entry = _unlinked_groups.pop(chat_id, None)
    now = datetime.now(UTC)
    if entry is None:
        entry = {"chat_id": chat_id, "first_seen_at": now, "message_count": 0}
    entry["last_seen_at"] = now
    entry["message_count"] += 1
    _unlinked_groups[chat_id] = entry
    while len(_unlinked_groups) > _UNLINKED_LIMIT:
        _unlinked_groups.popitem(last=False)


def recent_unlinked_groups() -> list[dict[str, Any]]:
    """Most recently seen first. Read by `GET /api/whatsapp/status`."""
    return list(reversed(list(_unlinked_groups.values())))


@router.post("/webhook")
async def gowa_webhook(
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
) -> dict[str, Any]:
    """Ingest one GOWA message event. Fast, idempotent, and 200 for everything retry cannot fix."""
    # (a) RAW BYTES FIRST, AND ONLY SO MANY OF THEM. Nothing above this line may parse them, and
    # nothing below it sees a body larger than MAX_BODY_BYTES. The signature is checked over the
    # exact bytes that were counted.
    raw = await read_capped_body(request)
    verify_signature(raw, request.headers.get(SIGNATURE_HEADER))

    # (b) Only now is it safe to spend cycles on the body. Read the event name FIRST, from a
    # model that validates nothing else: a group payload carries `chat_id` but no `id`, so
    # parsing it as a message fails and the join is discarded as "unparsable" — which is exactly
    # how group.joined got silently dropped. Route on the event, then parse for that shape.
    try:
        head = GowaEventHead.model_validate_json(raw)
    except ValidationError:
        log.warning("webhook.unparsable", extra={"byte_count": len(raw)})
        return _ignored("unparsable")

    if head.event in GROUP_EVENTS:
        return await _handle_group_event(raw, session, background)

    try:
        envelope = GowaWebhookEnvelope.model_validate_json(raw)
    except ValidationError:
        # Not a 422: the body is authentic (it was signed) but unusable, and five retries of an
        # unparsable body is five times the noise for the same outcome.
        log.warning("webhook.unparsable", extra={"byte_count": len(raw)})
        return _ignored("unparsable")

    if envelope.event and envelope.event != "message":
        return _ignored("unsupported_event")
    payload = envelope.payload
    if payload is None:
        log.warning("webhook.no_payload", extra={"event": envelope.event})
        return _ignored("unparsable")

    # (c) There is NO `is_group` field. The suffix is the entire group signal, and a direct
    # chat is out of scope for v1 — one linked group per household.
    if not payload.chat_id.endswith(GROUP_JID_SUFFIX):
        return _ignored("not_a_group")

    # (c2) THE LEDGER, FILLED FROM EVERY EVENT WE SEE. Before anything else this group might be
    # rejected for, write down that it exists. Most of the groups the paired account sits in
    # will never fire a join event for us — the account was already in them long before Penny
    # existed — so ordinary traffic is the only way we ever learn their ids, and a group we have
    # heard from can never later be mistaken for one we just joined. Ahead of the `is_from_me`
    # check on purpose: our own outbound message is still proof the group is old news.
    _remember_own_jid(envelope.device_id)
    await _observe_quietly(session, payload.chat_id)

    # Messages WE send come back to us with is_from_me=true and there is no filter on the GOWA
    # side. Ingesting them would attribute Penny's own outbound text to the family and feed it
    # straight back into extraction.
    if payload.is_from_me:
        return _ignored("from_me")

    if _is_mutation(payload):
        log.info("webhook.mutation_dropped", extra={"provider_message_id": payload.id})
        return _ignored("unsupported_message_kind")

    sent_at = _parse_timestamp(payload.timestamp)
    if sent_at is None:
        log.warning("webhook.bad_timestamp", extra={"provider_message_id": payload.id})
        return _ignored("bad_timestamp")

    raw_payload = _raw_payload(envelope, payload)
    message_type, text = _classify(payload)
    inbound = InboundMessage(
        provider="gowa",
        provider_message_id=payload.id,
        sender_wa_jid=_jid(payload),
        # Key identity on the @lid as well as the phone JID: WhatsApp is migrating to @lid and
        # keying on one field alone silently splits one person into two members.
        sender_wa_lid=_lid(payload),
        sender_display_name=payload.from_name,
        sent_at=sent_at,
        text=text,
        message_type=message_type,
        payload=raw_payload,
    )

    # (d) The seam resolves chat_id -> household. The handler never sees a household_id it did
    # not get from that lookup, which is what stops a webhook body naming a household.
    try:
        result = await ingest_messages(session, payload.chat_id, [inbound])
    except UnknownGroupError:
        # STORE NOTHING. SEND NOTHING. PROVISION NOTHING. 200 and a log line.
        #
        # THIS LINE IS THE INCIDENT. What used to be here was a call to `_onboard_group`, on the
        # theory that a credential posted back into the group is only readable by that group's
        # members and is therefore proof of membership. The theory was sound and the premise was
        # false: the paired number is a REAL WhatsApp account that was ALREADY a member of
        # several unrelated group chats, so "an unknown group sent us a message" did not mean
        # "someone just added Penny" — it meant "one of the strangers' chats this account has
        # been sitting in for months said something". Three households, two accidental, and a
        # login password posted into other people's conversations. A real person's words:
        # "It sent the message to all groups i'm in at the same time."
        #
        # An unlinked group is now indistinguishable from noise, whatever it says and whoever it
        # @-mentions. The ONLY way to become linked is `_handle_group_event`, i.e. an actual
        # join. `record_unlinked_group` keeps the chat id in memory so a human can link it by
        # hand from the settings screen — a hint on our side, not a word on theirs.
        record_unlinked_group(payload.chat_id)
        log.info(
            "webhook.unlinked_group_ignored",
            extra={"chat_id": payload.chat_id, "provider_message_id": payload.id},
        )
        return _ignored("unknown_group")

    log.info(
        "webhook.ingested",
        extra={
            "chat_id": payload.chat_id,
            "household_id": str(result.household_id),
            "provider_message_id": payload.id,
            "inserted": result.inserted,
            "duplicates": result.duplicates,
            "char_count": len(text or ""),
        },
    )

    # (e) Extraction is LLM work: seconds to minutes, and a hard dependency on OpenAI being up.
    # It goes to BackgroundTasks, which Starlette runs after the response has been sent. A
    # duplicate delivery adds no work here because `inserted` is 0 for a replay.
    if result.inserted:
        background.add_task(spawn_extraction, result.household_id, payload.id)

    # (f) The reply path, and the ONLY one. A linked group, a new message (not a replay), and an
    # explicit @-mention. `inserted` is what makes a GOWA retry free: five deliveries of one
    # mention must be one answer, not five.
    reply = "not_mentioned"
    if result.inserted:
        reply = await _maybe_schedule_reply(
            background,
            household_id=result.household_id,
            chat_id=payload.chat_id,
            provider_message_id=payload.id,
            text=text,
            raw_payload=raw_payload,
        )
    return {"status": "ok", "inserted": result.inserted, "reply": reply}


async def _handle_group_event(
    raw: bytes,
    session: AsyncSession,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """THE ONLY PATH THAT MAY EVER PROVISION OR SEND. Two gates, and both must open.

    A `group.joined` from whatsmeow does NOT mean "you were just added to a group". It also
    fires, once per group, during app-state sync — the burst that follows every reconnect, for
    every group the account was ALREADY in. GOWA's own source has no guard against re-emitting
    it, which is how one of the accidental households appeared: a container restart looked
    exactly like being invited to eight chats at once.

    Nothing in the event body distinguishes the two. So the guards are circumstantial, from
    `app.groups`, and independent:

      a. `observe_group()` — is this the first time we have EVER seen this group id? A group
         recorded by any earlier event, including an ordinary message, can never provision
         later. This is what makes the pre-existing chats permanently safe: the ledger fills
         from normal traffic, and a group in the ledger is old news forever.
      b. `in_startup_quiet_period()` — is the process old enough that a sync burst is over? App
         state lands within seconds of a connect, so a join in that window is recorded and NEVER
         provisioned, even if it is genuine.

    (b) is what covers the very first deploy, when the ledger is empty and (a) alone would wave
    every pre-existing group through. (a) is what covers every restart after it. THE GROUP IS
    RECORDED EITHER WAY, before either gate is consulted, so a refusal here also inoculates that
    id against every future join event.

    The cost is a genuine join in the first seconds after a deploy getting silence. That is the
    trade being made deliberately: a missed welcome is one message a human can send by hand, and
    a password in a stranger's chat is permanent.

    Only `group.joined` reaches the gates at all. `group.participants` fires on every membership
    change in every group Penny already sits in; it is subscribed so its chat ids reach the
    ledger, and it provisions nothing.

    Past the gates, the ordering trap is unchanged: `provision_for_group` does not commit,
    `get_session` does, and `spawn_welcome` waits for the household row to be readable on a
    fresh connection before it says a word. Committed first, credential second.
    """
    try:
        envelope = GowaGroupEnvelope.model_validate_json(raw)
    except ValidationError:
        log.warning("webhook.group_unparsable", extra={"byte_count": len(raw)})
        return _ignored("unparsable")

    payload = envelope.payload
    if payload is None:
        log.warning("webhook.no_payload", extra={"event": envelope.event})
        return _ignored("unparsable")

    # Same suffix rule as the message path: there is no `is_group` field anywhere in GOWA.
    if not payload.chat_id.endswith(GROUP_JID_SUFFIX):
        return _ignored("not_a_group")

    _remember_own_jid(envelope.device_id)

    # RECORD FIRST, DECIDE SECOND — and if the ledger cannot be written, decide "no". A gate we
    # failed to consult is not a gate that passed: an exception here (a missing table on a
    # half-run migration, a dead connection) must mean silence, not a household. Everything
    # below reads `first_sighting`, so there is no path to `provision_for_group` that skipped it.
    try:
        first_sighting = await observe_group(session, payload.chat_id)
    except Exception:
        log.exception("webhook.group_ledger_unavailable", extra={"chat_id": payload.chat_id})
        return _ignored("ledger_unavailable")

    if envelope.event != GROUP_JOINED_EVENT:
        # Recorded, and that is all a participants event is for.
        log.info(
            "webhook.group_participants_observed",
            extra={"chat_id": payload.chat_id, "first_sighting": first_sighting},
        )
        return _ignored("not_a_join_event")

    # GATE (b), checked first because it is free and because it is the one that holds when the
    # ledger is empty — a fresh database plus a reconnect is the exact shape of the incident.
    if in_startup_quiet_period():
        log.warning(
            "webhook.join_sync_suppressed",
            extra={
                "chat_id": payload.chat_id,
                "event": envelope.event,
                "first_sighting": first_sighting,
                "gate": "startup_quiet_period",
            },
        )
        return _ignored("sync_suppressed")

    # GATE (a). `observe_group` returned False, so some earlier event already named this group:
    # a message, a participants change, or a join we refused above. Whatever it was, we did not
    # just meet these people.
    if not first_sighting:
        log.warning(
            "webhook.join_already_known",
            extra={"chat_id": payload.chat_id, "event": envelope.event, "gate": "already_known"},
        )
        return _ignored("already_known")

    # GATE (c), THE CARDINALITY GATE. Gates (a) and (b) can both be open at once, and the state
    # in which that happens is the state production is in the day this ships: an empty ledger
    # makes every pre-existing group a first sighting, and a sync burst that lands a little late
    # clears the quiet window. Reproduced against the code as shipped — eight joins, eight
    # households, eight passwords. A human adds Penny to ONE group at a time, so a second group
    # appearing inside the window means app state, not an invitation. Counted in Postgres, so it
    # holds across both gunicorn workers; see `groups_first_seen_within`.
    #
    # This catches joins 2..N of a burst here, synchronously. Join 1 cannot be caught yet — it
    # is genuinely alone at this instant — so `send_welcome` holds it for the window and asks
    # again before it says anything. That is what makes the FIRST group of a burst safe too.
    burst_window = join_burst_window_seconds()
    appeared = 0
    if burst_window > 0:
        try:
            appeared = await groups_first_seen_within(session, burst_window)
        except Exception:
            # A gate we failed to consult is not a gate that passed.
            log.exception("webhook.join_burst_check_failed", extra={"chat_id": payload.chat_id})
            return _ignored("ledger_unavailable")
    if appeared > 1:
        log.warning(
            "webhook.join_burst_suppressed",
            extra={
                "chat_id": payload.chat_id,
                "event": envelope.event,
                "gate": "join_burst",
                "groups_appeared": appeared,
                "window_seconds": burst_window,
            },
        )
        return _ignored("burst_suppressed")

    provisioned = await provision_for_group(session, payload.chat_id)
    if provisioned is None:
        # Onboarding off, or the cap reached. Exactly the old behaviour: 200, no rows, silent.
        record_unlinked_group(payload.chat_id)
        log.warning(
            "webhook.unknown_group",
            extra={"chat_id": payload.chat_id, "event": envelope.event, "onboarded": False},
        )
        return _ignored("onboarding_declined")

    if not provisioned.created:
        # A concurrent delivery of the SAME join won the advisory lock and is sending the
        # welcome; a household that already had a link row would not have got past gate (a).
        # Either way there is no plaintext passphrase to send and nothing for us to do.
        log.info(
            "webhook.group_already_onboarded",
            extra={
                "chat_id": payload.chat_id,
                "event": envelope.event,
                "household_id": str(provisioned.household_id),
            },
        )
        return _ignored("already_linked")

    background.add_task(
        spawn_welcome,
        provisioned.household_id,
        payload.chat_id,
        provisioned.username,
        provisioned.passphrase,
    )
    log.info(
        "webhook.onboarded",
        extra={
            "chat_id": payload.chat_id,
            "event": envelope.event,
            "household_id": str(provisioned.household_id),
            "via": "group_event",
        },
    )
    return {"status": "ok", "onboarded": True}


async def spawn_welcome(household_id: UUID, chat_id: str, username: str, passphrase: str) -> None:
    """DETACH the send, for the same reason `spawn_extraction` detaches — see below.

    Returns immediately so the request can get on with committing the household this send
    depends on. Awaiting `send_welcome` here would be a deadlock on any Starlette that tears a
    `yield` dependency down after its background tasks: the send waits for a commit that is
    waiting for the send.
    """
    task = asyncio.create_task(send_welcome(household_id, chat_id, username, passphrase))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def send_welcome(household_id: UUID, chat_id: str, username: str, passphrase: str) -> None:
    """Post the credentials into the group, once the household is durably there.

    NOTHING ESCAPES, and nothing here is worth failing a webhook over. If GOWA is down, banned,
    or has no paired device, the household still exists and the family can be given the
    credentials another way — a failed welcome is a support conversation, a raised exception is
    a non-2xx that costs us five more deliveries of a message already stored.

    The passphrase is in the argument list and in the message body and NOWHERE ELSE. It is never
    logged, and `gowa.send_message` logs a character count rather than the text it sent.

    THE LAST THING THIS DOES BEFORE SPEAKING IS WAIT AND LOOK AGAIN. Gate (c) in
    `_handle_group_event` can only see the groups that have appeared SO FAR, so the first join
    of an app-state burst is indistinguishable from a real invitation at the moment it arrives —
    it is genuinely alone until the rest of the burst lands milliseconds later. Holding the
    welcome for the burst window and re-counting is what closes that gap, and it is the only
    thing standing between the first group of a burst and a password.

    So a genuine welcome is ~45s late. That is the entire cost, it is paid only once per family,
    and nobody adding a bot to a group chat is waiting on a stopwatch.
    """
    try:
        if not await _await_household(household_id):
            log.error(
                "webhook.welcome_household_never_committed",
                extra={"household_id": str(household_id), "chat_id": chat_id},
            )
            return
        if not await _was_a_solitary_join(household_id, chat_id):
            return
        message = welcome_message(username, passphrase, get_settings().app_public_url)
        result = await gowa.send_message(chat_id, message)
        if result.ok:
            log.info(
                "webhook.welcome_sent",
                extra={"household_id": str(household_id), "chat_id": chat_id},
            )
        else:
            # The household id, so an operator can find the family and hand over the
            # credentials by another route. The credentials themselves stay out of the log.
            log.error(
                "webhook.welcome_send_failed",
                extra={
                    "household_id": str(household_id),
                    "chat_id": chat_id,
                    "error": result.error,
                },
            )
    except Exception:
        log.exception("webhook.welcome_failed", extra={"household_id": str(household_id)})


async def _was_a_solitary_join(household_id: UUID, chat_id: str) -> bool:
    """Wait out the burst window, then decide whether this group joined ALONE. THE LAST GATE.

    Returns False — stay silent — for any answer that is not a confident yes, including a failed
    lookup. Silence costs a support message; the alternative cost a real person a password in a
    conversation they never invited Penny to.

    The household has already been provisioned by the time this runs, so refusing here leaves an
    orphan household nobody can log into. That is deliberate and it is the cheap half of the
    trade: an unused row an operator can delete, against a message that cannot be unsent. It is
    logged at ERROR with the id precisely so that clean-up is possible.
    """
    window = join_burst_window_seconds()
    if window <= 0:
        return True
    await asyncio.sleep(window)
    try:
        async with get_sessionmaker()() as session:
            # Twice the window: this group's own ledger row is now ~`window` seconds old, so a
            # single-window lookback would have already aged it out and under-count the burst.
            appeared = await groups_first_seen_within(session, window * 2)
    except Exception:
        log.exception("webhook.welcome_burst_check_failed", extra={"chat_id": chat_id})
        return False
    if appeared > 1:
        log.error(
            "webhook.welcome_burst_suppressed",
            extra={
                "household_id": str(household_id),
                "chat_id": chat_id,
                "groups_appeared": appeared,
                "window_seconds": window,
                "orphan_household": True,
            },
        )
        return False
    return True


# --- the ledger ------------------------------------------------------------------------


async def _observe_quietly(session: AsyncSession, chat_id: str) -> None:
    """Record a group id on the MESSAGE path, where failing to record must not lose a message.

    The join path deliberately does the opposite: there, an unwritable ledger means no
    household. Here the ledger is a side effect of ingesting, and refusing an ordinary family
    message because a bookkeeping row would not write is a real outage for no safety gain — the
    message path cannot provision or send anything under any circumstances.

    The SAVEPOINT is what makes "carry on" honest. A failed statement poisons the enclosing
    asyncpg transaction, so without `begin_nested` a swallowed error here would turn the ingest
    that follows into a confusing `InFailedSQLTransaction` several frames away.

    Note the asymmetry it creates and why it is safe: a group whose ledger write failed is NOT
    recorded, so a later join event would see `first_sighting=True` on it. The startup quiet
    period is the independent gate that still stands, and a ledger that is failing writes is
    failing loudly in the log above.
    """
    try:
        async with session.begin_nested():
            await observe_group(session, chat_id)
    except Exception:
        log.exception("webhook.observe_failed", extra={"chat_id": chat_id})


# --- @-mentions ------------------------------------------------------------------------


def _remember_own_jid(device_id: str | None) -> None:
    """Learn the paired number from the envelope GOWA just signed. No network call.

    Every event carries `device_id`, and it is the paired account's own JID. Taking it from here
    means the common case never touches the sidecar at all, and it is authenticated by the same
    HMAC as the rest of the body — this runs only after `verify_signature`.
    """
    global _own_jid, _own_jid_expires_at
    if normalise_jid(device_id) is None:
        return
    _own_jid = device_id
    _own_jid_expires_at = time.monotonic() + OWN_JID_TTL_SECONDS


def _device_jid(device: dict[str, Any]) -> str | None:
    """The paired phone JID from one `/devices` entry, whichever field this build puts it in."""
    for field in _DEVICE_JID_FIELDS:
        value = device.get(field)
        if isinstance(value, str) and normalise_jid(value) is not None:
            return value
    return None


async def resolve_own_jid() -> str | None:
    """The paired account's JID, cached, with a hard ceiling on how long it may block.

    Almost always answered from the cache that `_remember_own_jid` fills for free. The GOWA call
    is the cold-start fallback, and it is bounded twice over — a 2.5s `wait_for` inside a 10s
    handler budget, and a short negative cache so a dead sidecar costs that once a minute rather
    than once a message. Failure returns None, which downgrades mention detection rather than
    breaking ingest.
    """
    global _own_jid, _own_jid_expires_at
    now = time.monotonic()
    if _own_jid is not None and now < _own_jid_expires_at:
        return _own_jid
    async with _own_jid_lock:
        # Re-check: another request may have filled it while we queued here.
        now = time.monotonic()
        if _own_jid is not None and now < _own_jid_expires_at:
            return _own_jid
        try:
            devices, error = await asyncio.wait_for(
                gowa.list_devices(), timeout=OWN_JID_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # `asyncio.TimeoutError` is `TimeoutError` is an `Exception` on 3.11+, so the timeout
            # lands here too. `CancelledError` is a `BaseException` and deliberately does not:
            # a cancelled request should not be recorded as a failed lookup.
            log.warning("webhook.own_jid_lookup_failed", extra={"exc_type": type(exc).__name__})
            devices, error = None, "exception"
        jid = next((j for d in devices or [] if (j := _device_jid(d))), None)
        if jid is None:
            log.info("webhook.own_jid_unknown", extra={"error": error})
            _own_jid, _own_jid_expires_at = None, now + OWN_JID_FAILURE_TTL_SECONDS
            return None
        _own_jid, _own_jid_expires_at = jid, now + OWN_JID_TTL_SECONDS
        return jid


def _reply_allowed(household_id: UUID) -> bool:
    """Consume one reply slot for this household, or refuse.

    Consumed at SCHEDULING time, not at send time, because the cost being capped is the LLM call
    as much as the outbound message — a reply that turns out to be "say nothing" has still spent
    a completion. Sliding window over `REPLY_WINDOW_SECONDS`; in process, because the cap is a
    safety valve on a handful of households and a shared counter is not worth a second datastore
    (the same trade `app.ratelimit` documents at length).

    In-process means a multi-worker deploy allows up to `workers * MAX_REPLIES_PER_HOUSEHOLD`.
    Two workers, four each: still single digits an hour, still far below anything that looks
    like automation to WhatsApp.
    """
    now = time.monotonic()
    hits = _reply_hits.get(household_id)
    if hits is None:
        if len(_reply_hits) >= _REPLY_LIMIT_MAX_KEYS:
            _reply_hits.popitem(last=False)
        hits = _reply_hits[household_id] = deque()
    _reply_hits.move_to_end(household_id)
    cutoff = now - REPLY_WINDOW_SECONDS
    while hits and hits[0] <= cutoff:
        hits.popleft()
    if len(hits) >= MAX_REPLIES_PER_HOUSEHOLD:
        return False
    hits.append(now)
    return True


async def _maybe_schedule_reply(
    background: BackgroundTasks,
    *,
    household_id: UUID,
    chat_id: str,
    provider_message_id: str,
    text: str | None,
    raw_payload: dict[str, Any],
) -> str:
    """Decide whether this message earns an answer. Returns the reason, for the response body.

    Reached ONLY for a linked group and a newly inserted message — see the call site. The
    ordering is cheapest-first and that is not a micro-optimisation: `has_mention_marker` is pure
    string work and rejects every message without an `@`, which is nearly all of them, so
    `resolve_own_jid` is never reached on the hot path and the sidecar is never consulted for
    ordinary family chat.

    Nothing here calls the LLM or GOWA. It hands off to `BackgroundTasks`, because the handler
    owes GOWA a 200 well inside 10 seconds and an LLM call is seconds to minutes; blowing that
    budget means a retry, and a retry means the family gets the same answer twice.
    """
    if not has_mention_marker(text, raw_payload):
        return "not_mentioned"
    if not mentions_penny(text, raw_payload, await resolve_own_jid()):
        return "not_mentioned"
    if not _reply_allowed(household_id):
        # Silence, not an apology: an "I'm rate limited" message is itself an outbound message.
        log.warning(
            "webhook.reply_rate_limited",
            extra={
                "household_id": str(household_id),
                "chat_id": chat_id,
                "limit": MAX_REPLIES_PER_HOUSEHOLD,
                "window_seconds": REPLY_WINDOW_SECONDS,
            },
        )
        return "rate_limited"
    log.info(
        "webhook.reply_scheduled",
        extra={
            "household_id": str(household_id),
            "chat_id": chat_id,
            "provider_message_id": provider_message_id,
        },
    )
    background.add_task(spawn_reply, household_id, chat_id, provider_message_id, text or "")
    return "scheduled"


async def spawn_reply(
    household_id: UUID,
    chat_id: str,
    provider_message_id: str,
    text: str,
) -> None:
    """DETACH the reply, for the same reason `spawn_welcome` detaches — see there."""
    task = asyncio.create_task(send_reply(household_id, chat_id, provider_message_id, text))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def send_reply(
    household_id: UUID,
    chat_id: str,
    provider_message_id: str,
    text: str,
) -> None:
    """Answer an @-mention in the group it came from. NOTHING ESCAPES.

    Waits for the request to commit first, exactly as extraction does: the assistant reads the
    household's own record to answer, and a connection opened before the request committed
    cannot see the message being asked about — nor, on a first-message group, the household.

    `chat_id` is passed down from the delivery that triggered this and is never derived from the
    household, so there is no path by which an answer for one group is delivered to another. The
    household was resolved from that same chat_id by the ingest seam.
    """
    try:
        if not await _await_commit(household_id, provider_message_id):
            log.warning("webhook.reply_message_never_committed", extra={"chat_id": chat_id})
            return
        async with get_sessionmaker()() as session:
            household = await session.get(Household, household_id)
            if household is None:
                log.warning(
                    "webhook.reply_household_missing",
                    extra={"household_id": str(household_id)},
                )
                return
            answer = await answer_mention(session, household, text)
        if not answer:
            # "Nothing to say" is a first-class outcome — no OpenAI key, nothing relevant, an
            # answer the assistant declined to give. Saying nothing is always allowed.
            log.info("webhook.reply_declined", extra={"household_id": str(household_id)})
            return
        result = await gowa.send_message(chat_id, answer)
        log.info(
            "webhook.reply_sent" if result.ok else "webhook.reply_send_failed",
            extra={
                "household_id": str(household_id),
                "chat_id": chat_id,
                "char_count": len(answer),
                "error": result.error,
            },
        )
    except Exception:
        log.exception("webhook.reply_failed", extra={"household_id": str(household_id)})


async def spawn_extraction(household_id: UUID, provider_message_id: str) -> None:
    """DETACH the work from the request. Not gratuitous — see `extract_in_background`.

    Returns immediately so the request can get on with committing the message this run
    depends on.
    """
    task = asyncio.create_task(extract_in_background(household_id, provider_message_id))
    # asyncio holds only a WEAK reference to a running task; without this the garbage
    # collector may cancel an extraction mid-run, at random, under load.
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def extract_in_background(household_id: UUID, provider_message_id: str) -> None:
    """Run extraction after the 200 has gone out.

    Opens its **own** session: the request's session is closed by the `get_session` dependency
    as soon as the response is written, so using it here would touch a closed connection. That
    also makes this function the transaction owner, which is why it commits — implementation
    rule #1 ("handlers never commit") is about handlers, whose transaction `get_session` owns.

    IT MUST WAIT FOR THE REQUEST TO COMMIT FIRST. On FastAPI 0.140 / Starlette 1.3 a `yield`
    dependency is torn down AFTER the response's background tasks run, so `get_session`'s
    commit happens *last*. A task that starts work immediately opens a second connection that
    cannot see the message that triggered it, reads `messages_considered=0`, and logs a
    cheerful `ran=True events=+0`. Observed, not theorised: the first live webhook of the
    acceptance run ingested a message and extracted nothing 5ms later.

    That failure is quiet and permanent-ish. A replay inserts 0 rows so it schedules no
    second run, which means the message waits for the NEXT new message or the cron tick — the
    feed silently lags the conversation by one message.

    Nothing escapes. An exception here has no request to fail and no client to tell. The
    messages stay `extracted_at IS NULL`, so the next webhook or the cron tick picks them back
    up — that column is the entire retry mechanism and it needs no help from us.
    """
    try:
        if not await _await_commit(household_id, provider_message_id):
            # The request rolled back after all; there is no message and nothing to extract.
            log.warning(
                "webhook.message_never_committed",
                extra={"household_id": str(household_id)},
            )
            return
        async with get_sessionmaker()() as session:
            try:
                summary = await run_extraction_for_household(session, household_id)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
        log.info(
            "webhook.extraction_done",
            extra={"household_id": str(household_id), "summary": str(summary)},
        )
    except Exception:
        log.exception("webhook.extraction_failed", extra={"household_id": str(household_id)})


async def _await_commit(household_id: UUID, provider_message_id: str) -> bool:
    """Block until the ingested message is visible from a fresh connection."""
    return await _await_visible(
        sa.select(Message.id).where(
            Message.household_id == household_id,
            Message.provider_message_id == provider_message_id,
        )
    )


async def _await_household(household_id: UUID) -> bool:
    """Block until the provisioned household is visible from a fresh connection.

    This is the ordering guarantee behind the welcome message: the family never learns a
    password before the household it opens is durable.
    """
    return await _await_visible(sa.select(Household.id).where(Household.id == household_id))


async def _await_visible(statement: sa.Select[Any]) -> bool:
    """True once the row the request is writing has been committed by it.

    Milliseconds in practice; the ceiling only matters when the request failed and will never
    commit at all, in which case we give up rather than act on a write that never happened.
    """
    maker = get_sessionmaker()
    for _ in range(COMMIT_WAIT_TRIES):
        async with maker() as session:
            found = (await session.execute(statement)).first()
        if found is not None:
            return True
        await asyncio.sleep(COMMIT_WAIT_SECONDS)
    return False


def _ignored(reason: str) -> dict[str, Any]:
    return {"status": "ignored", "reason": reason}


def _is_mutation(payload: GowaMessagePayload) -> bool:
    """Best-effort detection of an edit, deletion or reaction. Shape unverified — see above."""
    if (payload.type or "").lower() in _MUTATION_TYPES:
        return True
    if (payload.action or "").lower() in _MUTATION_TYPES:
        return True
    extra = payload.model_extra or {}
    return any(key in extra and extra[key] for key in _MUTATION_KEYS)


def _classify(payload: GowaMessagePayload) -> tuple[str, str | None]:
    """`(message_type, text)`.

    v1 stores the type for media and **nothing else** — no caption, no download. That is not an
    oversight: `WHATSAPP_AUTO_DOWNLOAD_MEDIA=false` means we never hold the file, and a caption
    without its image reads as a non-sequitur in the feed and as a hallucination risk in a
    prompt ("here it is" attached to nothing).
    """
    # `extra="allow"` means pydantic exposes unmodelled keys as attributes too, so one getattr
    # covers both the fields declared above and the media types GOWA adds after this was written.
    for key, kind in MEDIA_FIELDS.items():
        if getattr(payload, key, None):
            return kind, None
    declared = (payload.type or "").lower()
    if declared in MEDIA_FIELDS:
        return MEDIA_FIELDS[declared], None
    text = payload.body if payload.body is not None else payload.text
    return "text", text or None


def _jid(payload: GowaMessagePayload) -> str | None:
    """The phone-number JID, if `from` is one. A `@lid` sender has no phone JID to record."""
    sender = payload.from_
    return sender if sender and sender.endswith("@s.whatsapp.net") else None


def _lid(payload: GowaMessagePayload) -> str | None:
    """`from_lid` when present; otherwise `from` if WhatsApp gave us a @lid instead of a phone."""
    if payload.from_lid:
        return payload.from_lid
    sender = payload.from_
    return sender if sender and sender.endswith("@lid") else None


def _raw_payload(envelope: GowaWebhookEnvelope, payload: GowaMessagePayload) -> dict[str, Any]:
    """The payload as GOWA sent it, plus which device delivered it.

    Built from `model_dump` on a model with `extra="allow"`, so fields this file never heard of
    survive into the column. Re-extraction reads this, so it is never normalised on the way in
    — anything we "tidy" now is information a future prompt cannot get back.
    """
    dumped = payload.model_dump(by_alias=True, mode="json")
    if envelope.device_id:
        dumped["_device_id"] = envelope.device_id
    return dumped


def _parse_timestamp(value: Any) -> datetime | None:
    """Epoch seconds, epoch milliseconds, or ISO 8601 — all normalised to tz-aware UTC.

    The runbook captured an ISO string and the API contract records an epoch int; both shapes
    are real. A naive datetime is rejected by `InboundMessage` itself, which is why this always
    attaches UTC rather than letting the server's local zone leak into a health record.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return _from_epoch(float(value))
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return _from_epoch(float(candidate))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _from_epoch(value: float) -> datetime | None:
    if value <= 0:
        return None
    if value > _MS_THRESHOLD:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None
