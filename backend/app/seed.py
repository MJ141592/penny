"""Create a household and print its credentials, exactly once.

    uv run python -m app.seed --username the-shaws --name "The Shaws" \
        --care-recipient Margaret --timezone Europe/London

There is no signup screen and never will be — a care-coordination app for one family is
provisioned by whoever set it up. This is that provisioning step.

**The passphrase is generated, not chosen.** A human-chosen password on the one shared
credential that guards a health record is the single likeliest way this app is broken into, and
there is no second factor and no per-user account to contain the blast radius. `secrets` picks
it; the operator's only job is to copy it. It is printed once and never recoverable, because it
is stored as an argon2 hash and nothing else — losing it means `--reset-password`, which is the
correct shape for a credential store.

Idempotent on `--username`: re-running never clobbers an existing household's password, so it
is safe to put in a deploy script.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import secrets
import sys
from zoneinfo import available_timezones

import sqlalchemy as sa

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import Household
from app.security import hash_password

# Short, unambiguous, no homophones or near-homophones to mistype when read down a phone.
_WORDS_RAW = (
    "amber anchor apple arrow autumn basil beacon birch bishop bramble breeze bridge butter"
    " cactus candle canvas cedar cello cinder cobalt comet copper coral cotton crimson crystal"
    " damson daisy dapple delta denim dexter diesel dinner dollar domino donkey dragon drift"
    " eagle ember emerald engine escape ethos exodus fabric falcon fennel ferry fiddle figure"
    " filter flannel flint forest fossil fountain garnet gecko ginger glacier glimmer granite"
    " gravel guitar hammer harbour hazel heather helix hollow honey hunter indigo ingot ivory"
    " jackal jasmine jersey jigsaw jungle juniper kestrel kettle kindle lantern lattice lemon"
    " lichen lilac linen lobster locket lumber magnet mango maple marble marrow meadow mellow"
    " mercury minnow mirror mitten monsoon mortar mosaic muffin mulberry mustard nectar nickel"
    " nimbus nomad nutmeg oatmeal ocean olive onyx opal orbit orchid osprey otter oxide oyster"
    " paddle pantry parcel parsnip pebble pelican pepper pewter pigeon pilot pincer pistol"
    " pocket pollen poplar portal possum powder prairie pretzel prism pudding pumpkin puzzle"
    " quarry quartz quiver radish rafter rally rapid rattle raven ribbon rocket rooster rubble"
    " ruby saddle saffron sailor salmon sandal sapphire satchel scarlet scooter shadow shovel"
    " signal silver siren sketch slate sleigh snorkel socket sorrel spiral sprout stable"
    " stencil sterling stirrup summit sundial sunset syrup tandem tangle tapestry teapot"
    " tempo tender thicket thimble thistle thunder timber tinder toffee token topaz torrent"
    " tractor trellis trumpet tulip tundra turnip tusk umber ushering valley vanilla velvet"
    " vessel violet vulture walnut wander whistle willow window winter wombat yarrow yonder"
)
# Deduped, because a repeated word would make the entropy figure printed below a lie.
WORDS = sorted(set(_WORDS_RAW.split()))

PASSPHRASE_WORDS = 6
DEFAULT_HOUSEHOLD_NAME = "The Family"


def slugify_username(name: str) -> str:
    """`"Doyle family"` -> `"doyle-family"`.

    So `--username` is optional. Asking an operator to invent a second identifier for the one
    household they are creating is a step that only exists to be typo'd, and the typo is
    invisible: it silently creates a SECOND household rather than being idempotent on the
    first. Derived from the name, it is stable across re-runs by construction.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().casefold()).strip("-")
    return slug or DEFAULT_HOUSEHOLD_NAME.casefold().replace(" ", "-")


def generate_passphrase(word_count: int = PASSPHRASE_WORDS) -> str:
    """`secrets.choice`, never `random` — `random` is a Mersenne Twister and is predictable
    from a handful of prior outputs, which is a real problem for a seeder that may be run
    repeatedly during a deploy."""
    words = [secrets.choice(WORDS) for _ in range(word_count)]
    # Two digits on the end so the string satisfies "must contain a number" password policies
    # in password managers and browser autofill heuristics, and adds ~6.6 bits for free.
    return "-".join(words) + f"-{secrets.randbelow(100):02d}"


def passphrase_entropy_bits(word_count: int = PASSPHRASE_WORDS) -> float:
    return word_count * math.log2(len(set(WORDS))) + math.log2(100)


def _validate_timezone(name: str) -> str:
    """Checked here as well as in the model, so a typo fails on the command line with a usable
    message instead of as a SQLAlchemy validation error mid-transaction."""
    if name not in available_timezones():
        raise SystemExit(
            f"error: {name!r} is not a known timezone. "
            "Use an IANA name such as Europe/London or America/New_York."
        )
    return name


def _print_credentials(*, username: str, passphrase: str, household_name: str) -> None:
    rule = "=" * 68
    print(rule)
    print("  PENNY CREDENTIALS — shown once, not recoverable.")
    print(rule)
    print(f"  household : {household_name}")
    print(f"  username  : {username}")
    print(f"  passphrase: {passphrase}")
    print(f"  strength  : ~{passphrase_entropy_bits():.0f} bits of entropy")
    print(rule)
    print("  Store it in the family's password manager now. Only the argon2 hash is kept,")
    print("  so this cannot be printed again — use `--reset-password` to issue a new one.")
    print(rule)


async def _seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.database_url:
        print("error: DATABASE_URL is not set.", file=sys.stderr)
        return 2

    timezone = _validate_timezone(args.timezone or settings.default_timezone)

    async with get_sessionmaker()() as session:
        existing = (
            await session.execute(sa.select(Household).where(Household.username == args.username))
        ).scalar_one_or_none()

        if existing is not None and not args.reset_password:
            # Idempotent on username. Explicitly NOT rotating the password here: this command
            # runs in deploy scripts, and a silent rotation would sign the family out of an
            # app they were using, with the new credential buried in a build log.
            print(
                f"Household {args.username!r} already exists ({existing.id}); nothing to do.\n"
                "Pass --reset-password to issue a new passphrase."
            )
            return 0

        if existing is None and not args.care_recipient:
            # --reset-password against a username that does not exist. Creating one here would
            # need a care recipient we were never given, and NOT NULL would fail mid-flush.
            print(
                f"error: no household with username {args.username!r}. "
                "Drop --reset-password and pass --care-recipient to create one.",
                file=sys.stderr,
            )
            return 2

        passphrase = generate_passphrase()
        password_hash = hash_password(passphrase)

        if existing is not None:
            existing.password_hash = password_hash
            household_name = existing.name
            print(f"Reset the passphrase for {args.username!r} ({existing.id}).")
        else:
            household = Household(
                username=args.username,
                password_hash=password_hash,
                name=args.name or DEFAULT_HOUSEHOLD_NAME,
                care_recipient_name=args.care_recipient,
                timezone=timezone,
            )
            session.add(household)
            await session.flush()
            household_name = household.name
            print(f"Created household {args.username!r} ({household.id}).")

        # This CLI is not a request handler, so nothing else owns the transaction boundary —
        # it commits for itself. The rule about handlers never committing is about get_session.
        await session.commit()

    _print_credentials(username=args.username, passphrase=passphrase, household_name=household_name)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Create a Penny household with a generated passphrase.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Login username, e.g. the-shaws. Defaults to a slug of --name.",
    )
    parser.add_argument("--name", default=None, help='Household name, e.g. "The Shaws".')
    parser.add_argument(
        "--care-recipient",
        dest="care_recipient",
        default=None,
        help="The person being cared for, e.g. Margaret. Required when creating.",
    )
    parser.add_argument(
        "--timezone", default=None, help="IANA timezone. Defaults to DEFAULT_TIMEZONE."
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Issue a new passphrase for an existing household.",
    )
    args = parser.parse_args(argv)
    if args.care_recipient is None and not args.reset_password:
        parser.error("--care-recipient is required when creating a household.")
    if not args.username:
        if not args.name:
            parser.error("pass --username, or --name to derive one from.")
        args.username = slugify_username(args.name)
    return args


async def _run(args: argparse.Namespace) -> int:
    # The pool MUST be disposed inside the same event loop that opened it. A second
    # `asyncio.run(dispose_engine())` closes asyncpg's sockets against a loop that is already
    # gone and dumps a "Event loop is closed" traceback over the credentials we just printed.
    try:
        return await _seed(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
