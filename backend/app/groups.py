"""The join-safety ledger: is this group NEW, and is this process old enough to believe it?

THIS MODULE EXISTS BECAUSE PENNY POSTED A PASSWORD INTO STRANGERS' GROUP CHATS.

The WhatsApp account Penny is paired to is a real account that was already a member of several
unrelated group chats. Onboarding provisioned a household on ANY message from ANY unknown group,
so the welcome message — which contains a login and a password — went out to all of them at
once. A real person wrote: "It sent the message to all groups i'm in at the same time, but i'm
not sure why". Three households were created, two of them accidental.

The fix is that a welcome is sent ON A GENUINE JOIN AND ON NOTHING ELSE, and these two
functions are what "genuine" is decided with. They answer the two questions a join event cannot
answer for itself:

    observe_group()          Have we ever seen this group before? A group Penny has been
                             sitting in for months is not one she was just added to, however
                             the event is labelled.
    in_startup_quiet_period() Is this process young enough that a burst of join events is
                             app-state sync rather than a human adding her to a chat?

THE QUIET PERIOD IS NOT A HACK AND MUST NOT BE DELETED. whatsmeow (inside GOWA) re-emits
JoinedGroup for groups it ALREADY belonged to while it replays app-state after a connect. Every
deploy reconnects. So on every deploy, GOWA emits what looks exactly like "Penny was just added
to this chat" for every pre-existing group at once — which is precisely how a password reached
a conversation nobody meant to invite her to. There is no field on the event that distinguishes
the two cases; the only signal available is that the burst arrives seconds after the socket
came up. Hence a window of process life in which no join is trusted at all.

THE COST IS DELIBERATE AND IS THE RIGHT WAY ROUND. A family who add Penny during the first
three minutes after a deploy get no welcome, and a human has to send them their credentials.
That is a support message. The failure it buys off is a password in a stranger's chat, which
cannot be unsent, cannot be un-read, and is the reason onboarding is currently disabled in
production. When in doubt, stay silent.

Both signals are advisory to the caller and neither one sends anything: this module records and
reports, the webhook decides. Nothing here commits — `get_session` owns the transaction.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.models import KnownGroup

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# The fallback if `STARTUP_QUIET_PERIOD_SECONDS` is ever missing from Settings. Three minutes
# comfortably covers whatsmeow's app-state replay, which lands within seconds of the socket
# coming up; the margin is free, because the only thing it delays is a welcome that a human can
# send by hand.
DEFAULT_STARTUP_QUIET_PERIOD_SECONDS = 180.0

# The fallback if `JOIN_BURST_WINDOW_SECONDS` is missing from Settings. Long enough to span an
# app-state replay, which delivers its groups within seconds of each other; short enough that
# two unrelated families adding Penny inside the same window is a coincidence nobody will meet.
# The price of the window is paid on every genuine join — see `send_welcome`, which holds the
# welcome for this long before deciding it was alone.
DEFAULT_JOIN_BURST_WINDOW_SECONDS = 45.0

# MONOTONIC, not wall clock: a container's clock gets stepped by NTP shortly after boot, which
# is exactly the window this measures, and a backwards step on a wall clock would extend the
# quiet period indefinitely or end it instantly. Module import happens during app startup, a
# few hundred milliseconds after the process began — close enough for a three-minute window,
# and the error is in the safe direction (the window is measured from slightly later, so it
# ends slightly later).
_PROCESS_STARTED_AT = time.monotonic()


def process_age_seconds() -> float:
    """Seconds since this module was imported, i.e. since the process came up."""
    return time.monotonic() - _PROCESS_STARTED_AT


def quiet_period_seconds() -> float:
    """The configured window, defaulting safely.

    Read through `getattr` because `Settings` is not this module's file to edit: if the field
    is absent, the answer must be "the default window", never an AttributeError raised out of a
    webhook and never a silent zero. A missing setting must not mean "no quiet period" — that
    is the failure this whole module exists to prevent.

    Zero or negative disables the window, which is what tests and a deliberate "we are paired
    to a fresh number, let joins through" incident setting both want.
    """
    value = getattr(
        get_settings(), "startup_quiet_period_seconds", DEFAULT_STARTUP_QUIET_PERIOD_SECONDS
    )
    return float(value)


def in_startup_quiet_period() -> bool:
    """True while a burst of join events is more likely app-state sync than a real join.

    See the module docstring before changing or removing this. It is the difference between a
    deploy being a deploy and a deploy being a password broadcast.
    """
    window = quiet_period_seconds()
    if window <= 0:
        return False
    return process_age_seconds() < window


def join_burst_window_seconds() -> float:
    """The window in which two new groups appearing at once means "sync", not "invited".

    Read through `getattr` for the same reason `quiet_period_seconds` is: a missing field must
    mean the default window, never an AttributeError out of a webhook and never a silent zero.
    Zero or negative disables the burst guard, which is what the tests that are about the other
    two gates want, and what a fresh number that belongs to no other groups can afford.
    """
    value = getattr(get_settings(), "join_burst_window_seconds", DEFAULT_JOIN_BURST_WINDOW_SECONDS)
    return float(value)


async def groups_first_seen_within(session: AsyncSession, seconds: float) -> int:
    """How many groups have entered the ledger in the last `seconds`. THE BURST SIGNAL.

    WHY THIS EXISTS, WHEN THERE ARE ALREADY TWO GATES. Both of the others can be open at the
    same time, and the state in which that happens is the state production is in on the day
    this ships. `observe_group` only knows what it has been told, so on a brand-new ledger
    EVERY pre-existing group is a "first sighting"; and `in_startup_quiet_period` only covers
    the first few minutes of process life, so an app-state burst that lands a little late clears
    it. Fresh ledger plus a late burst is exactly the original incident, and it was reproducible
    against the shipped code: eight `group.joined` events, eight households, eight passwords.

    The signal the other two gates do not use is CARDINALITY. A human adds Penny to one group at
    a time — that is the whole of what "being invited" looks like. whatsmeow replays app state
    for every group the account already belonged to, all at once. So several never-before-seen
    groups appearing together is sync by definition, whatever the events are labelled.

    COUNTED IN POSTGRES, NOT IN MEMORY, because the deployment runs `--workers 2`: an
    in-process counter would see roughly half of any burst, and a burst of two would look like
    two separate solitary joins, one per worker. `first_seen_at` is already on the row and is
    already write-once, so this needs no new column and no migration — and it survives the
    worker restart that would reset any in-process tally.

    It counts ledger arrivals rather than joins specifically, which is deliberate and errs the
    safe way. A group whose first-ever event is an ordinary message counts too; that is still a
    group appearing out of nowhere, which is still the shape being guarded against. The cost is
    that a genuine join landing in the same 45 seconds as some stranger group's first-ever
    message is refused a welcome. That is one support message, and each stranger group can only
    ever contribute one row, once, forever.

    Uses the DATABASE's clock (`now()`) on both sides of the comparison, so a container whose
    wall clock is being stepped by NTP cannot widen or collapse the window.
    """
    if seconds <= 0:
        return 0
    # `now() - (:seconds * interval '1 second')` rather than a cast of a formatted string: the
    # driver binds a float and Postgres does the arithmetic, so there is no text interval to
    # parse and no locale in which "45.0 seconds" means something else.
    cutoff = sa.func.now() - (sa.literal(float(seconds)) * sa.text("interval '1 second'"))
    return int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(KnownGroup)
            .where(KnownGroup.first_seen_at > cutoff)
        )
        or 0
    )


async def observe_group(session: AsyncSession, group_external_id: str) -> bool:
    """Record the group, and return True only the FIRST time it has ever been seen.

    "First time" is decided by whether a row was INSERTED, not by a SELECT that is then acted
    on. Several webhook deliveries for the same group land at once — GOWA retries five times on
    any non-2xx and a busy group delivers in parallel — and a check-then-insert would let two
    of them both read "not there" and both conclude they were first. Two firsts is two welcome
    messages carrying two different passwords.

    `ON CONFLICT DO NOTHING ... RETURNING` collapses that to a single statement the database
    arbitrates: the loser blocks on the winner's uncommitted row, then finds the conflict and
    returns nothing. Exactly one caller sees True, per group, ever.

    Never commits — `get_session` owns the transaction. That also means the truthful reading of
    True is "first time, IF this transaction commits": a handler that rolls back leaves the
    group unrecorded and the next event is first again. That is the safe direction. The row is
    a claim that Penny has met this group, and a rolled-back request did not.
    """
    inserted = await session.execute(
        pg_insert(KnownGroup)
        .values(group_external_id=group_external_id)
        .on_conflict_do_nothing(index_elements=["group_external_id"])
        .returning(KnownGroup.group_external_id)
    )
    first_seen = inserted.scalar_one_or_none() is not None
    if first_seen:
        # Ids only, and no message text ever. This line is the audit trail for "why did / did
        # not Penny greet that group", which is the first question asked after an incident.
        log.info("groups.first_seen", extra={"group_external_id": group_external_id})
    return first_seen


async def is_known_group(session: AsyncSession, group_external_id: str) -> bool:
    """Have we ever recorded this group? A pure read — it never records anything itself.

    Separate from `observe_group` because the caller that only wants to ASK must not be able to
    accidentally make the answer True. Use this to reject a join event for a group Penny has
    been in for months; use `observe_group` on the path that is allowed to claim it.
    """
    return bool(
        await session.scalar(
            sa.select(sa.literal(True)).where(
                sa.exists().where(KnownGroup.group_external_id == group_external_id)
            )
        )
    )
