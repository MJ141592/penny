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

# THE SIZE OF THIS LIST IS THE PASSWORD STRENGTH, so treat it as a security parameter and not
# as decoration. A passphrase is FOUR words drawn from it (`PASSPHRASE_WORDS`), which is 41.4
# bits: log2(1296) = 10.34 bits a word, 1296^4 = 2.8e12 combinations. Against the login rate
# limit (10 attempts / 15 minutes) that is ~8 billion years of guessing, so the online attack
# this actually faces is not a threat; the offline one is argon2's problem, not the list's.
#
# It used to be six words plus two digits from a 236-word list. Four words is what a family can
# retype from a phone, and dropping the digits was the same call — but four words of 236 is only
# 31.5 bits, which is thin. The strength was bought back by GROWING THE LIST rather than by
# adding characters people get wrong: 236 -> 1296 words is +2.5 bits a word, and four words of
# 1296 beats six words of 236 while being a third shorter to type.
#
# EFF-style selection rules, all of which are load-bearing:
#   * 3-8 letters, lowercase a-z only, so it types on a phone keyboard with no modifier keys
#   * no homophones or near-homophones (no beech/beach, cellar/seller, chord/cord) — the
#     credential gets read down a phone to a relative who is not in the group
#   * no US/UK spelling splits (no harbour/harbor, yogurt/yoghurt, mollusk/mollusc)
#   * no silent-letter or commonly-misspelled words (no wren, knapsack, rhythm, yacht)
#   * nothing that reads badly beside an elderly relative's name in a chat they can all see:
#     no symptoms (dizzy), no falls (tumble, shuffle), no funeral flowers (lily, wreath), no
#     words that land as an insult next to someone's name (ancient, shrew, jumbo)
# Adding words only ever helps. REMOVING them weakens every password issued afterwards, which
# is why the count is asserted at import below rather than trusted to review.
_WORDS_RAW = (
    "abbey acacia acorn acrobat active adder agate agile airplane airport airship alcove alder "
    "alert alley almond alpaca alphabet amber amble amethyst ample anagram anchor anchovy "
    "anecdote anorak antelope anthem antler anvil aphid applaud applause apple apricot apron "
    "aqua aqueduct arcade archway arctic armchair arrow aspen asteroid atlas atoll atom atrium "
    "attic auburn aurora autumn avenue avocado award awning axis axle azure backpack badge "
    "badger bagel bakery balcony ballad ballet balloon bamboo banana bandana banister banjo "
    "banner bargain barge barley barn barnacle barrel basalt basil basin basket bassoon bathtub "
    "bazaar beacon beagle beanbag beanie beaver bedside beehive beetle beetroot beige bench "
    "bicycle biplane birch biscuit bison blanket blazer blender blizzard blossom bluebell "
    "bluebird boat bobcat bold bolt bonfire bonnet bookcase bookmark bookshop boot bottle "
    "boulder bounce bouquet bowl bracelet bracken bracket bramble branch brave breeze breezy "
    "bridge bright brisk bristle bronze brooch brook broom brownie brush bubble bubbly bucket "
    "buckle buffalo buggy bugle bulb bulldog bulletin bullfrog bundle bunker bunting butter "
    "button cabbage cabin cabinet cable cactus calendar calico calm camel camera campfire "
    "campus canal canary candle candy canister canoe canvas canyon caramel caravan carbon "
    "cardigan cardinal caribou carnival carpet carriage cartoon cascade cashew castle cauldron "
    "causeway cavern cedar celery cello ceramic chair chalk channel chapel chapter charcoal "
    "chariot charm chatter cheddar cheer cheerful cheese cheetah cherry chestnut chickpea chime "
    "chipmunk chirpy chisel chive chorus chowder chrome chuckle cicada cider cilantro cinema "
    "cinnamon circus civic clam clarinet classic clatter clever cliff climb clipper cloak "
    "closet cloud clover cluster coaster cobalt cobra cobweb cockatoo cockle cocoa coconut "
    "cocoon coffee collar college comet compass concerto condor confetti conifer convoy "
    "cookbook cookie copper corduroy cork corridor cosmic cosmos costume cottage cotton couch "
    "cougar cove coyote crab crafty crane crater crayon crescent cricket crimson crisp crumb "
    "crumpet crystal cuckoo cucumber cuddle cufflink cupboard cupcake curly curtain curved "
    "cushion custard custom cutlery daffodil dahlia dainty daisy damson dance daring daybreak "
    "daydream daylight deep delta denim diamond diary dinghy dingo dockyard dogwood dolphin "
    "dome donkey doodle dozen drawer dream dreamy dresser drift drill drizzle drum drumbeat "
    "duckling dumpling dune dusk dustpan dusty eager eagle early earring earthy earwig easel "
    "echo eclipse eggplant egret elegant elephant embers emblem emerald endive engine envelope "
    "equator eraser errand essay estuary etching even exotic explore fable fabric falcon fancy "
    "fanfare fearless feather fedora fennel fern ferret ferry festival festive fiddle fiery "
    "fiesta finch firefly firework fjord flagpole flagship flamingo flannel flask fleece flint "
    "flounder flourish fluffy flurry flute flutter foam folder folklore fond forest fortress "
    "fortune fossil foxglove foyer freckle freight fresco frigate frolic frost frosty funnel "
    "furnace gadget galaxy galleon gallery garage garden gardenia garland garlic garnet gasket "
    "gateway gather gazebo gazelle gecko gemstone gentle geranium gerbil gesture geyser gibbon "
    "giddy gift giggle ginger gingham ginkgo giraffe glacier glad glade gleeful glide glimmer "
    "glimpse glitter glossy glove glowworm goblet goggles golden gondola goose gopher gorge "
    "gorse graceful granary grand grandeur granite granola grape grassy gravel gravity gravy "
    "greeting grotto grouse grove guava guitar gulf gully gumdrop guppy gypsum halibut hallway "
    "hammer hammock hamster handbook handy happy harmony harp harvest hawk hawthorn hazel "
    "hazelnut hearth hearty heather hedgehog helium heron herring hibiscus highland hillside "
    "hinge hippo hobby hoist holiday holly homemade honest honey honeydew hook horizon hornet "
    "hostel humble hyacinth ibex iceberg idea igloo iguana impala indigo island ivory jackal "
    "jackdaw jacket jade jaguar jamboree jasmine jelly jersey jetty jigsaw jog jolly journal "
    "joyful jubilee jug juice jumble jungle juniper kale kayak keen keepsake kestrel ketchup "
    "kettle keyboard kimono kindly kindness kitchen kite kitten kiwi koala ladder ladybug "
    "lagoon lake landmark lantern larch lark latte lattice laughter launch laurel lava lavender "
    "leafy leap leather ledger lemon lemonade lemur lentil leopard lesson letter lettuce lever "
    "library lichen lifeboat lilac lime limerick linden linen lively lizard llama lobby lobster "
    "locker locket locust lodge lofty lookout lotus loyal lucky luggage lullaby lunar lyric "
    "macaw mackerel magenta magic magnet magnolia magpie mahogany mailbox mallard mallet "
    "manatee mandolin mango mangrove manor mansion manta mantis maple maraca marathon marble "
    "marigold marker market marlin marmot maroon marsh martin marvel marzipan mascot mattress "
    "mayfly meadow medley meerkat mellow melody melon memento memory mercury merry mesa message "
    "meteor midge mighty mild mill mimosa mineral mingle minnow minty miracle mirror misty "
    "mitten mixer mixture moccasin mocha modern modest moment mongoose monkey monsoon monument "
    "mosaic moss moth motto mountain mouse muddy muesli muffin mulberry mule mural museum "
    "mushroom mustard myrtle mystery mystic napkin narwhal neat nebula necklace nectar needle "
    "nettle neutron newt nickel nifty nightcap nightjar nimble noble noodle notebook notion "
    "nova nozzle nugget number nursery nutmeg oaken oasis oatmeal oboe obsidian ocean octave "
    "octopus offer okra oleander olive onion onyx opal opera orange orbit orchard orchid "
    "oregano organ osprey ostrich otter ottoman outing oven overall oxygen oyster package "
    "paddle pageant palace pancake panda pansy panther pantry papaya paper paprika parade "
    "parakeet parcel parka parrot parsley parsnip passport pasta pastel pastime pastry pasture "
    "pattern pavilion peacock peanut pearl pebble pecan pelican pencil penguin pennant peony "
    "pepper peppy perch perky pesto petunia pewter pheasant phrase piano pickle picnic pigeon "
    "piglet pigment pike pillow pine pinwheel pitcher pizza plaid planet plank planner planter "
    "plaster plateau platinum platter playbook plaza pliers plucky plunger plush plywood pocket "
    "poem polite poncho pond ponder pontoon pony poodle popcorn poplar porch porridge portrait "
    "possum postcard poster potato pottery prairie prawn present pretzel primrose prism promise "
    "prompt proton proud pudding puffer puffin pulley pulsar puma pumpkin puzzle pyramid python "
    "quail quaint quarry quartet quick quiet quilt quince quiver rabbit raccoon radar radiator "
    "radish raft rafter railway rainbow raincoat raisin rake rally ramble ranch rapid ratchet "
    "raven ravine ravioli ready recipe recorder redwood reef regal reindeer reunion rhino "
    "rhubarb ribbon riddle ridge risotto river robin robust rocker rocket rooftop rook rope "
    "rosy rowboat ruby rudder rug rugged ruler rumble runway rustic sable saddle saffron sage "
    "sailboat salmon sample sandal sandwich sandy sapling sapphire sardine sarong satchel satin "
    "saucer saunter savanna sawmill scallion scallop scamper scarf scarlet schooner scissors "
    "scone scooter screw scribble seagull seahorse sequel sequoia serene sesame session shady "
    "shallot shamrock sharp shawl sheep shelf shimmer shiny shoelace shovel shrimp shutter "
    "sidecar sierra sieve signal silica silkworm silky silo silver simple skate sketch skillet "
    "skink skylark slate sled sleek slender slipper slogan smooth smoothie snapper snappy "
    "snapshot sneaker snowball snowdrop snowfall snowy sock sofa solar solid solstice sonata "
    "sonnet sorbet soup souvenir sparkle sparrow spatula spicy spinach spindle spire sponge "
    "spool spoon sprinkle sprint sprout spruce squash squid squiggle squirrel stable stadium "
    "stallion stanza stapler starfish starling starship station statue steady steeple stencil "
    "stool stopper stork story stove strainer stream string stripe stroll strudel studio sturdy "
    "sturgeon suede sugar suitcase summit sunbeam sundial sunlit sunny sunrise sunset super "
    "surprise swallow swamp swan sweater swirl sycamore symphony syrup table tablet taco "
    "tadpole tandem tape tapestry tapioca tarragon tartan tavern taxi teak teamwork teapot "
    "teaspoon temple tempo tender termite terrace terrier theory thermos thicket thimble "
    "thistle thrifty thrush thunder tiara ticket tickle tidy tiger timber timeline timely "
    "tinker tinsel toffee tomato tongs tonic toolbox topaz torch tortilla tortoise toucan towel "
    "tower township toybox tractor trailer train tram tranquil trawler treasure treetop trellis "
    "triangle tricycle trinket tripod triumph trolley trombone trophy trot trousers trout "
    "trowel trumpet trunk trusty tuba tugboat tulip tumbler tuna tundra tunic tunnel turban "
    "turmeric turnip turret turtle tuxedo tweezers twilight twinkle twirl ukulele umbrella "
    "unicorn unicycle universe upbeat upgrade urban urchin vacation valley vanilla vantage vase "
    "velvet venture veranda viaduct vibrant victory village vine vineyard vintage viola violet "
    "violin viper vivid volcano voltage voyage waffle wagon wallet walnut walrus wander warbler "
    "wardrobe warm warmth wasabi washer wasp wavy waxwing weasel weevil welcome wetland wheat "
    "whimsy whisk whistle wicker wiggle willow windmill window windy wise wisteria witty wombat "
    "wonder woodland woolly workshop worthy yarn yarrow yodel young zany zebra zenith zephyr "
    "zesty zigzag zinc zinnia zipper zucchini"
)
# Deduped and frozen. A repeated word would make the entropy figure below a lie, and a tuple
# because nothing should ever be able to shrink this at runtime.
WORDS = tuple(sorted(set(_WORDS_RAW.split())))

# The floor the security argument above rests on. Checked at IMPORT, not in a test: a test can
# be deleted in the same commit that trims the list, and the failure mode of a quietly smaller
# list is invisible — every password issued afterwards is weaker and nothing looks different.
MIN_WORDS = 1296
if len(WORDS) < MIN_WORDS:
    raise RuntimeError(
        f"app.seed.WORDS has {len(WORDS)} words, below the {MIN_WORDS} the passphrase "
        "strength is specified against. Add words; never remove them."
    )

# FOUR words, per the user, and no digits. See the note on the word list: the size of the list
# is what pays for the shortness of the passphrase.
PASSPHRASE_WORDS = 4
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
    """`"willow-thistle-copper-lagoon"`. Four words, hyphens, NO DIGITS.

    `secrets.choice`, never `random` — `random` is a Mersenne Twister and is predictable from a
    handful of prior outputs, which is a real problem for a seeder that may be run repeatedly
    during a deploy.

    The trailing two digits this used to carry are gone deliberately. They bought 6.6 bits and
    cost more than that in the real failure mode: this string is retyped from a phone by
    someone reading it out of a WhatsApp message, and a digit group is where the transcription
    breaks. All of the strength is in the word list now, which is the parameter that can be
    grown without making the credential harder to type.
    """
    return "-".join(secrets.choice(WORDS) for _ in range(word_count))


def passphrase_entropy_bits(word_count: int = PASSPHRASE_WORDS) -> float:
    """41.4 bits at the default: 4 x log2(1296). Computed, never hardcoded, so the number
    printed to an operator tracks the list they are actually drawing from."""
    return word_count * math.log2(len(WORDS))


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
