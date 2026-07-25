"""`/api/whatsapp/status`, `/link` and `/relink` — the pairing surface, behind the session cookie.

**Losing the WhatsApp session is routine, not exceptional.** whatsmeow sessions die: WhatsApp
expires linked devices, the account gets an "at risk" warning, someone taps "log out" on the
phone. The plan treats a re-pair as a normal thing a family does, which is why `/status`
reports link health as data rather than as an error, and why `/relink` exists as a first-class
route instead of a runbook step. If re-pairing needed an operator, ingest would be dead from
the moment the session dropped until somebody noticed.

`/status` therefore reports **three independent facts**, and the UI needs all three to say
anything useful:

| | meaning | what the UI shows |
|---|---|---|
| `linked` | Penny has a `whatsapp_links` row | "no group linked yet" -> link form |
| `is_connected` | GOWA has a live socket | transient; a deploy flaps this |
| `is_logged_in` | the WhatsApp session is alive | false -> **re-pair by QR** |

Collapsing them loses the distinction between "the sidecar is restarting" (wait) and "the
session is gone" (fetch a phone). Per the contract, an unreachable GOWA reports both booleans
`false` and is **not** an error: the settings page renders, it just says not connected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import gowa
from app.deps import CurrentHousehold, HouseholdCtx, SessionDep
from app.errors import ConflictError
from app.ingest.contract import GROUP_JID_SUFFIX
from app.models import WhatsappLink
from app.routers.webhooks import recent_unlinked_groups

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

NOT_A_GROUP_DETAIL = "That is not a group chat id (it must end in @g.us)."
ALREADY_LINKED_DETAIL = "That group is already linked."
LOGIN_BUSY_DETAIL = "A pairing code is already being requested. Try again in a moment."

# `/app/login` blocks for up to 120 seconds inside GOWA. Without this, a family tapping the
# re-pair button twice holds two two-minute requests open against a sidecar that can only be
# pairing one device anyway. One in flight at a time; the second gets an immediate, honest 409.
_login_lock = asyncio.Lock()


class UnlinkedGroup(BaseModel):
    """A group that messaged us while unlinked — a candidate for the link form."""

    chat_id: str
    message_count: int
    first_seen_at: datetime
    last_seen_at: datetime


class WhatsappStatus(BaseModel):
    linked: bool
    group_external_id: str | None = None
    is_connected: bool = False
    is_logged_in: bool = False
    # Extra beyond the contract's four fields, both additive. `gowa_available` separates "the
    # sidecar did not answer" from "the sidecar answered, and the session is dead".
    gowa_available: bool = False
    unlinked_groups: list[UnlinkedGroup] = Field(default_factory=list)


class LinkRequest(BaseModel):
    group_external_id: str


class RelinkResponse(BaseModel):
    """`qr_link` is a **PNG URL**. Render it in an `<img>`; do not pass it to a QR encoder."""

    available: bool
    device_id: str | None = None
    qr_link: str | None = None
    qr_duration: int | None = None
    error: str | None = None


@router.get("/status", response_model=WhatsappStatus)
async def whatsapp_status(session: SessionDep, ctx: CurrentHousehold) -> WhatsappStatus:
    """Link state plus sidecar session health. Polled at 30s on the settings screen only."""
    link = await _current_link(session, ctx)
    health = await gowa.get_status()
    return WhatsappStatus(
        linked=link is not None and link.status == "linked",
        group_external_id=link.group_external_id if link else None,
        is_connected=health.is_connected,
        is_logged_in=health.is_logged_in,
        gowa_available=health.available,
        # Only shown when the household has nothing linked: once they are ingesting, a list of
        # other people's chat ids is noise at best.
        unlinked_groups=[] if link else [UnlinkedGroup(**g) for g in recent_unlinked_groups()],
    )


@router.post("/link", status_code=status.HTTP_204_NO_CONTENT)
async def link_group(body: LinkRequest, session: SessionDep, ctx: CurrentHousehold) -> None:
    """Bind a WhatsApp group to this household. This row IS the tenant boundary for ingest.

    `whatsapp_links.group_external_id` is globally unique, which is what makes resolution inside
    the seam safe: no two households can claim the same group, so a webhook body naming a
    `chat_id` can only ever reach the one household that linked it.
    """
    group_external_id = body.group_external_id.strip()
    # There is no `is_group` field anywhere in GOWA's message events. Checking the suffix at the
    # boundary means a typo'd direct-chat JID is caught by a sentence in the UI rather than by
    # silence — a linked direct chat would simply never receive a message the webhook accepts.
    if not group_external_id.endswith(GROUP_JID_SUFFIX):
        raise HTTPException(status_code=400, detail=NOT_A_GROUP_DETAIL)

    owner = await session.scalar(
        sa.select(WhatsappLink).where(WhatsappLink.group_external_id == group_external_id)
    )
    if owner is not None and owner.household_id != ctx.id:
        # Note this is a 409 and not a 404: the group id came from the caller and its
        # uniqueness is a global constraint they can observe by trying, so refusing plainly
        # tells them nothing about the other household. There is no row of theirs being named.
        log.warning("whatsapp.link_conflict", extra={"household_id": str(ctx.id)})
        raise ConflictError(ALREADY_LINKED_DETAIL)

    link = await _current_link(session, ctx)
    if link is None:
        session.add(
            WhatsappLink(
                household_id=ctx.id,
                group_external_id=group_external_id,
                status="linked",
                linked_at=datetime.now(UTC),
            )
        )
    else:
        # Idempotent when it is the same group (the family retried, or double-tapped), and a
        # move when it is a different one — a household that starts a new care group must be
        # able to point Penny at it without an operator. One group per household is enforced by
        # `whatsapp_links.household_id` being the primary key, so this is an update, not a
        # second row.
        link.group_external_id = group_external_id
        link.status = "linked"
        link.linked_at = datetime.now(UTC)
    # No commit: `get_session` owns the transaction and commits when this returns.
    log.info("whatsapp.linked", extra={"household_id": str(ctx.id), "chat_id": group_external_id})


@router.post("/relink", response_model=RelinkResponse)
async def relink(ctx: CurrentHousehold) -> RelinkResponse:
    """Request a fresh pairing QR.

    **This request can take up to two minutes.** GOWA's `/app/login` blocks until whatsmeow
    produces the first QR, and that is normal, not a hang — the client must show a spinner and
    must never put this on a poll. It is a separate route from `/status` precisely so the thing
    the settings page polls every 30 seconds cannot inherit a 120-second timeout.

    The QR PNG is served by GOWA **outside its basic auth** (gotcha 8), on a UUID filename that
    is deleted after ~30 seconds. That is tolerable for a link the family opens immediately and
    is the reason `qr_duration` is passed through: the UI should count down and re-request
    rather than leave a stale URL on screen.
    """
    if _login_lock.locked():
        raise ConflictError(LOGIN_BUSY_DETAIL)
    async with _login_lock:
        login = await gowa.start_login()
    log.info(
        "whatsapp.relink_requested",
        extra={"household_id": str(ctx.id), "available": login.available, "error": login.error},
    )
    return RelinkResponse(
        available=login.available,
        device_id=login.device_id,
        qr_link=login.qr_link,
        qr_duration=login.qr_duration,
        error=login.error,
    )


async def _current_link(session: AsyncSession, ctx: HouseholdCtx) -> WhatsappLink | None:
    """Always filtered on `ctx.id`. There is no RLS; this filter is the isolation."""
    return await session.scalar(sa.select(WhatsappLink).where(WhatsappLink.household_id == ctx.id))
