"""Provision a household the first time Penny is added to a WhatsApp group.

THIS FILE DELIBERATELY REVERSES A RULE THAT USED TO BE ABSOLUTE. `app.ingest.seam` and the
webhook both said it: an unknown group NEVER auto-provisions a household, because a webhook body
that can name a chat id could otherwise mint tenants at will. That warning was right about the
mechanism and wrong about the mitigation, and the new one is stronger:

**The credential is posted INTO the group, and nowhere else.** Only members of that group can
read it, so provisioning is proof-of-possession by construction. An attacker who forges a chat id
they are not in creates a household whose password is delivered to a conversation they cannot
see — they have burned a slot on the cap and learned nothing. That is also the fix the security
review asked for against a group being claimed by the wrong tenant: the claim and the proof are
the same message. Note the shape of the guarantee — it is the *webhook signature* that makes the
chat id trustworthy at all (`verify_signature` runs before any of this), and this function that
makes possession of the group the only way to reach the account.

What is left to defend is not data, it is the OpenAI bill: every household runs extraction, so
anyone who learns the WhatsApp number can mint households and starve real families of budget.
`ONBOARDING_MAX_HOUSEHOLDS` is the ceiling on that, and `ONBOARDING_ENABLED=false` is the switch
that puts the old silent-200 behaviour back. They are the two levers, in that order.

THE OTHER HALF IS IDEMPOTENCY, and it is the half that actually bites. GOWA retries five times
with exponential backoff on any non-2xx, and a busy group delivers several messages before the
first has finished. Unprotected, that is five households and five welcome messages carrying five
different passwords into a family's chat on the day they meet the product. So:

    a. a pre-check outside the lock, because the steady state is "already provisioned"
    b. `pg_advisory_xact_lock` over a hash of the group id, so concurrent deliveries queue
    c. a RE-CHECK inside the lock — the first writer commits while the second waits, and
       READ COMMITTED gives that second transaction a fresh snapshot for this statement
    d. the global unique index on `whatsapp_links.group_external_id` as the backstop, because
       an advisory lock is cooperative and a future caller may forget to take it

First writer wins; everyone else gets `created=False` and the caller sends nothing.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import Household, WhatsappLink
from app.security import hash_password

# The passphrase generator and its word list live in `app.seed`, which is where the only other
# credential in this product is minted. Importing it (rather than copying the list) is what
# keeps "the thing a family types on a phone" a single, reviewable definition of strength —
# ~54 bits, no homophones. `app.seed` is import-safe: everything below its `main()` is behind
# the `__main__` guard.
from app.seed import DEFAULT_HOUSEHOLD_NAME, WORDS, generate_passphrase

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Two words and two digits: ~250^2 * 100 ≈ 6M, against a cap of tens of households. It is an
# identifier, not a secret — readability wins, because someone types it on a phone.
USERNAME_WORDS = 2
# Collisions are vanishingly unlikely; exhausting eight of them means something else is wrong.
USERNAME_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class Provisioned:
    """The outcome of one provisioning attempt.

    `passphrase` is PLAINTEXT and exists nowhere else — only the argon2 hash is stored. It is
    returned here, put in the welcome message, and never logged. When `created` is False the
    household already existed and the plaintext is unrecoverable, so the field is `""`: there
    is nothing to send, which is exactly what the caller should do.
    """

    household_id: UUID
    username: str
    passphrase: str
    created: bool


def generate_username() -> str:
    """`"cedar-thistle-04"`. Readable, sayable over the phone, and not a UUID."""
    words = [secrets.choice(WORDS) for _ in range(USERNAME_WORDS)]
    return "-".join(words) + f"-{secrets.randbelow(100):02d}"


def advisory_lock_key(group_external_id: str) -> int:
    """A stable signed int64 for `pg_advisory_xact_lock`.

    `hash()` would be wrong here: it is salted per process, so two workers would take two
    different locks for the same group and serialise nothing. blake2b is stable across
    processes and deploys. A collision between two unrelated groups costs one of them a short
    wait and nothing else.
    """
    digest = hashlib.blake2b(group_external_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def provision_for_group(session: AsyncSession, group_external_id: str) -> Provisioned | None:
    """Create (or find) the household that owns `group_external_id`.

    None means "stay silent": onboarding is switched off, or the cap is reached. Never commits —
    `get_session` owns the transaction, and the advisory lock is released when it does.
    """
    settings = get_settings()
    if not settings.onboarding_enabled:
        log.info("onboarding.disabled", extra={"group_external_id": group_external_id})
        return None

    # (a) The steady state, after the first delivery, is a group that already has a household.
    # Answering it without taking a lock keeps the hot path off the serialisation point.
    existing = await _existing_for_group(session, group_external_id)
    if existing is not None:
        return existing

    # argon2 is ~50ms of blocking CPU. Done BEFORE the lock so it is not held for it, and after
    # the pre-check so a routine retry never pays for it.
    passphrase = generate_passphrase()
    password_hash = hash_password(passphrase)

    # (b) Concurrent deliveries for this group now queue here, and unblock at commit/rollback.
    await _take_lock(session, group_external_id)

    # (c) The whole point of the lock: whoever waited sees the winner's committed row.
    existing = await _existing_for_group(session, group_external_id)
    if existing is not None:
        log.info(
            "onboarding.already_provisioned",
            extra={
                "group_external_id": group_external_id,
                "household_id": str(existing.household_id),
            },
        )
        return existing

    household_count = await session.scalar(sa.select(sa.func.count()).select_from(Household))
    cap = settings.onboarding_max_households
    if (household_count or 0) >= cap:
        log.warning(
            "onboarding.cap_reached",
            extra={
                "group_external_id": group_external_id,
                "household_count": household_count,
                "onboarding_max_households": cap,
            },
        )
        return None

    return await _create(
        session,
        group_external_id=group_external_id,
        passphrase=passphrase,
        password_hash=password_hash,
    )


async def _take_lock(session: AsyncSession, group_external_id: str) -> None:
    """Serialise concurrent deliveries for ONE group, for the rest of this transaction.

    Transaction-scoped (`_xact_`), never session-scoped: there is no unlock call to forget and
    no way for a handler that raises to leave the lock held. `get_session` ends the transaction
    either way, and the lock goes with it.
    """
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                # Cast, because pg_advisory_xact_lock is overloaded on (bigint) and (int, int)
                # and an untyped parameter leaves the function call ambiguous.
                sa.cast(advisory_lock_key(group_external_id), sa.BigInteger)
            )
        )
    )


async def _existing_for_group(session: AsyncSession, group_external_id: str) -> Provisioned | None:
    """The household already bound to this group, if any. `created=False`, no plaintext."""
    row = (
        await session.execute(
            sa.select(WhatsappLink.household_id, Household.username)
            .join(Household, Household.id == WhatsappLink.household_id)
            .where(WhatsappLink.group_external_id == group_external_id)
        )
    ).first()
    if row is None:
        return None
    return Provisioned(
        household_id=row.household_id,
        username=row.username,
        passphrase="",
        created=False,
    )


async def _create(
    session: AsyncSession,
    *,
    group_external_id: str,
    passphrase: str,
    password_hash: str,
) -> Provisioned | None:
    """Insert the household and its link, retrying only a username collision.

    Each attempt is a SAVEPOINT, because an IntegrityError poisons the surrounding transaction
    and the caller (a webhook) still has a 200 to write. (d): if the unique index on
    `group_external_id` is what rejected us, another delivery won between the re-check and the
    flush and the answer is `created=False`, not a retry.
    """
    settings = get_settings()
    for _ in range(USERNAME_ATTEMPTS):
        username = generate_username()
        household = Household(
            username=username,
            password_hash=password_hash,
            name=DEFAULT_HOUSEHOLD_NAME,
            # NOT NULL, and nobody has told us who is being cared for yet. Still holding this
            # exact string is how the app knows first-run setup has not happened.
            care_recipient_name=settings.onboarding_placeholder_care_recipient,
            timezone=settings.default_timezone,
        )
        try:
            async with session.begin_nested():
                session.add(household)
                await session.flush()
                session.add(
                    WhatsappLink(
                        household_id=household.id,
                        group_external_id=group_external_id,
                        status="linked",
                        linked_at=datetime.now(UTC),
                    )
                )
                await session.flush()
        except IntegrityError:
            lost = await _existing_for_group(session, group_external_id)
            if lost is not None:
                log.info(
                    "onboarding.lost_race",
                    extra={
                        "group_external_id": group_external_id,
                        "household_id": str(lost.household_id),
                    },
                )
                return lost
            # Not the group index, so it was the username. Draw another slug.
            log.info(
                "onboarding.username_collision", extra={"group_external_id": group_external_id}
            )
            continue

        # Ids and counts only. The passphrase is never logged, and the username is never logged
        # in the same breath as one: together they are the credential.
        log.info(
            "onboarding.provisioned",
            extra={
                "group_external_id": group_external_id,
                "household_id": str(household.id),
            },
        )
        return Provisioned(
            household_id=household.id,
            username=username,
            passphrase=passphrase,
            created=True,
        )

    log.error(
        "onboarding.username_attempts_exhausted",
        extra={"group_external_id": group_external_id, "attempts": USERNAME_ATTEMPTS},
    )
    return None


def welcome_message(username: str, passphrase: str, public_url: str) -> str:
    """The first thing a family ever sees from this product.

    Plain text: WhatsApp has no markdown, and asterisks meant as emphasis render as literal
    asterisks around a password someone then types in.

    Two lines are here for reasons that outlive the copy. The message CONTAINS a password, and
    WhatsApp history is retroactive — anyone added to this group next year can scroll back and
    read it — so that has to be said plainly rather than discovered. And it has to say the
    password can be changed, or the only remedy anyone knows about is a new group.

    It also ASKS FOR CONTEXT, in the chat, right at the start. That is not politeness: the care
    recipient's name and background are the single largest input to extraction quality, because
    they are what turns "she had a bad night again" into a fact about a named person, and what
    stops a sibling's own dentist appointment being filed as care. Asking in the group beats
    waiting for someone to fill in a form, because the answer arrives as an ordinary message and
    is therefore ingested and visible to extraction from that moment on.

    Asked as three specific questions rather than "tell me about them" — an open prompt gets a
    one-word reply, and the third question is where the durable facts live (conditions,
    medications, the GP's name) that later messages assume you already know.
    """
    url = public_url.rstrip("/")
    return (
        "Hello! I'm Penny.\n"
        "\n"
        "I turn this group's day-to-day updates into one shared timeline of symptoms, "
        "appointments and medications, so nobody has to scroll back through months of chat to "
        "work out what happened.\n"
        "\n"
        "Your family's account is ready:\n"
        "\n"
        f"{url}\n"
        f"Username: {username}\n"
        f"Password: {passphrase}\n"
        "\n"
        "To get me started, could someone reply here with:\n"
        "\n"
        "1. Who you're looking after, and what you call them in this chat\n"
        "2. Roughly their age, and how they live day to day\n"
        "3. Anything ongoing worth knowing — conditions, medications, their GP or hospital\n"
        "\n"
        "That last one matters more than it sounds. It is what lets me understand a message "
        'like "she had a bad night again" months from now, instead of guessing who "she" is. '
        "You can also add it at Settings on the website.\n"
        "\n"
        "Two things worth knowing: this message contains your password, so anyone added to this "
        "group later can scroll back and read it, and you can change it in Settings whenever "
        "you like."
    )
