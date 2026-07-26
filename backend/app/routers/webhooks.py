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

3. **An unknown group is onboarded, not rejected.** Penny added to a group provisions a
   household for it and posts the credentials back into the group. See the long comment on the
   `UnknownGroupError` path for why that is not the tenant leak the previous rule guarded
   against, and `_onboard_group` for the ordering it forces: household committed first, welcome
   message second, never the other way round.

4. **Idempotency is not implemented here.** It lives in the partial unique index on
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
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import gowa
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import SessionDep
from app.errors import PennyError, UnauthorizedError
from app.extraction.service import run_extraction_for_household
from app.ingest.contract import (
    GROUP_JID_SUFFIX,
    InboundMessage,
    IngestResult,
    UnknownGroupError,
)
from app.ingest.seam import ingest_messages
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
# onboarding never fires — which is exactly how this shipped broken. These are the two events
# that mean "we are in a new group", per GOWA's own README table.
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


def _is_self_join(envelope: GowaGroupEnvelope, payload: GowaGroupPayload) -> bool:
    """Did WE just get added to this group, as opposed to someone else joining it?

    `group.joined` means it by definition — GOWA emits it with `jids` set to our own JID.
    `group.participants` fires for every membership change in every group we are already in, so
    provisioning on it unconditionally would mint a household every time a cousin joins a chat
    Penny has been sitting in for months. It only counts when the action is a join AND our own
    JID is among the affected — and `device_id` on the envelope is that JID.
    """
    if envelope.event == GROUP_JOINED_EVENT:
        return True
    if (payload.type or "").lower() != "join":
        return False
    own = (envelope.device_id or "").split(":")[0].split("@")[0]
    return bool(own) and any(own == jid.split(":")[0].split("@")[0] for jid in payload.jids)


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

    # Messages WE send come back to us with is_from_me=true and there is no filter on the GOWA
    # side. Ingesting them would attribute Penny's own outbound text to the family and feed it
    # straight back into extraction.
    if payload.is_from_me:
        return _ignored("from_me")

    # (c) There is NO `is_group` field. The suffix is the entire group signal, and a direct
    # chat is out of scope for v1 — one linked group per household.
    if not payload.chat_id.endswith(GROUP_JID_SUFFIX):
        return _ignored("not_a_group")

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
        # 200, always. Nothing about retrying an unlinked group can make it linked.
        #
        # THIS IS WHERE AUTO-PROVISIONING USED TO BE FORBIDDEN, and the reversal is deliberate.
        # The old rule read "an unknown group NEVER auto-provisions a household — that is the
        # tenant-leak vector", and against a webhook that simply created a household it was
        # right. What makes it safe now is WHERE THE CREDENTIAL GOES: the passphrase is revealed
        # in exactly one place, a WhatsApp message posted back INTO that group. Only members of
        # the group can read it, so holding the credential is proof of membership — which is
        # also the answer the security review wanted to "what stops the wrong tenant claiming a
        # group?". Someone who forges a webhook naming a stranger's chat_id causes a household
        # to be created whose password is delivered to the stranger's group and never to them.
        # The original boundary is untouched: a webhook body still cannot NAME a household. It
        # can only cause one to exist for the group the message actually came from.
        result = await _onboard_group(session, background, payload.chat_id, inbound)
        if result is None:
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
    return {"status": "ok", "inserted": result.inserted}


async def _handle_group_event(
    raw: bytes,
    session: AsyncSession,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Penny was added to a group: provision the household and post the credentials.

    The message path's twin, minus the message — a join carries no text to ingest, so this
    provisions and sends and nothing else. Everything else is deliberately identical, including
    the ordering trap: `provision_for_group` does not commit, `get_session` does, and
    `spawn_welcome` waits for the household row to be readable on a fresh connection before it
    says a word. Committed first, credential second.

    Idempotency is the same guarantee too. GOWA retries five times on a non-2xx, and a join can
    arrive alongside the first messages; whoever loses the advisory lock gets `created=False`
    and sends nothing, so a family gets one password rather than five.
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

    if not _is_self_join(envelope, payload):
        return _ignored("not_a_self_join")

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
        # A message from this group already provisioned it; that delivery owns the welcome.
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


async def _onboard_group(
    session: AsyncSession,
    background: BackgroundTasks,
    chat_id: str,
    inbound: InboundMessage,
) -> IngestResult | None:
    """Provision a household for a group nobody has linked, then ingest the message anyway.

    Returns None ONLY for "stay silent and answer 200 with no rows": onboarding is switched off,
    or the cap is reached. That is the pre-onboarding behaviour, unchanged.

    The message that introduced Penny to the group is RE-INGESTED rather than dropped. It is
    usually the one that says why the group added her ("adding Penny so we can all keep track of
    Mum's appointments"), and dropping it would make the first thing Penny ever sees the second
    thing the family ever said.

    PROVISIONING AND SENDING ARE TWO DECISIONS, NOT ONE. `created=False` means a concurrent
    delivery provisioned this group microseconds ago and committed while we waited on the
    advisory lock. It is the reason not to send a SECOND password into the family's chat — and
    it is NOT a reason to drop this message, because the household it belongs to now exists and
    the link is committed and visible. Treating one flag as the answer to both questions loses
    real messages: three people typing the moment Penny joins produced one stored row and two
    200s that discarded the text — permanently, because a 200 is exactly what stops GOWA
    retrying. Measured, not theorised: 3 concurrent deliveries, 1 message kept.
    """
    provisioned = await provision_for_group(session, chat_id)
    if provisioned is None:
        # Exactly today's behaviour: 200, no rows, no message.
        record_unlinked_group(chat_id)
        log.warning("webhook.unknown_group", extra={"chat_id": chat_id, "onboarded": False})
        return None

    # FLUSH before re-ingesting. The sessionmaker sets autoflush=False, so the household and
    # its whatsapp_links row are still pending Python objects; the seam resolves the group with
    # a SELECT, and an unflushed INSERT is invisible to it. Without this the re-ingest would
    # raise UnknownGroupError a second time and the message really would be dropped. (On the
    # `created=False` path there is nothing pending and the flush is a no-op; the row it needs
    # was committed by the delivery that won.)
    #
    # If the re-ingest raises anyway, it is deliberately NOT swallowed. That is the one shape of
    # failure a retry genuinely fixes: the whole transaction rolls back, the household goes with
    # it, nothing was sent, and GOWA's next delivery starts clean. Answering 200 here instead
    # would leave a family with a household and no credentials and no message.
    await session.flush()
    result = await ingest_messages(session, chat_id, [inbound])

    if not provisioned.created:
        # Someone else's request is sending the welcome. Ours only had a message to save.
        log.info(
            "webhook.joined_existing_onboarding",
            extra={
                "chat_id": chat_id,
                "household_id": str(provisioned.household_id),
                "inserted": result.inserted,
            },
        )
        return result

    # THE ORDERING TRAP: THE HOUSEHOLD MUST BE COMMITTED BEFORE THE PASSWORD GOES OUT. Sending
    # from inside the request would hand a family a credential for a household that a later
    # rollback means never existed — and the failure is silent on our side and permanent on
    # theirs, because the message cannot be unsent. So the ordering is not left to whether
    # Starlette runs background tasks before or after `get_session`'s teardown (the two have
    # traded places between versions — see `extract_in_background`, and on FastAPI 0.140 /
    # Starlette 1.3.1 the commit is observably first). This task only SPAWNS, and `send_welcome`
    # waits for the household row to be readable on a fresh connection before it says a word.
    # Committed first, credential second, under either ordering.
    #
    # It is a background task at all, rather than an await here, because a send is a network
    # call to a sidecar that can hang. Ten seconds of hanging GOWA inside the handler is a GOWA
    # retry, and a retry is a second delivery of a message we have already stored.
    background.add_task(
        spawn_welcome,
        provisioned.household_id,
        chat_id,
        provisioned.username,
        provisioned.passphrase,
    )
    log.info(
        "webhook.onboarded",
        extra={
            "chat_id": chat_id,
            "household_id": str(provisioned.household_id),
            "inserted": result.inserted,
        },
    )
    return result


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
    """
    try:
        if not await _await_household(household_id):
            log.error(
                "webhook.welcome_household_never_committed",
                extra={"household_id": str(household_id), "chat_id": chat_id},
            )
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
