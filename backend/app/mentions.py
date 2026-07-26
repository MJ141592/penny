"""Did this message @-mention Penny?

**Penny answers only when she is spoken to.** The paired WhatsApp number is a real account that
sits in groups nobody linked, and outbound volume is what gets a number banned, so "reply to
everything in a linked group" is not on the table. An explicit @-mention is the whole trigger,
and this module is the only place that decides what one is.

WHAT A MENTION ACTUALLY LOOKS LIKE ON THE WIRE — captured in production, not guessed:

    "@17473209317 who are you"

The WhatsApp client renders a contact name; the message BODY carries the bare number. So the
primary signal is text, and `mentioned_jid`-style metadata is a bonus we check when it is there
and never depend on: the GOWA message payload shape for mentions is unverified, and a mention
detector that only works when an unverified field is present is a detector that never fires.

TWO DELIBERATE ASYMMETRIES:

*Generous about formatting, strict about the number.* A mention may arrive as `@17473209317`,
`@+1 747 320 9317` or `@1747-320-9317`; all of those are the same person. So the digits are
compared after every separator is stripped, and a match is allowed on a common suffix — a group
member's client may render the number with or without a country code and we cannot tell which
from here. `_MIN_DIGITS` is what stops "a common suffix" from meaning "@1 matches everything".

*A false negative is cheap; a false positive is not.* A missed mention is a family repeating
themselves. A spurious one is Penny talking uninvited — so nothing here matches on "Penny"
appearing in a sentence, only on an explicit `@`. The literal `@penny` alias is included
because it is what someone types when the client did not turn their mention into a number, and
it is still an unambiguous act of address.

This module NEVER decides whether to send anything. It answers one question about one message;
the webhook owns "is this group linked", "have we replied too often", and "does Penny have
anything to say". In an unlinked group the answer here is not even consulted.
"""

from __future__ import annotations

import re
from typing import Any

# `@` then an optional `+`, then a run of phone-ish characters. The run may contain the
# separators a human or a client might insert (spaces, dashes, dots, brackets) so that a
# formatted number survives to the digit comparison below — the run stops at the first character
# that cannot be part of a phone number, which is how "@17473209317 who are you" yields the
# number and not the sentence.
_MENTION_RE = re.compile(r"@\s*\+?\s*([0-9][0-9\s\-().]{4,30})")

# Below this, a "shared suffix" is a coincidence rather than a number. WhatsApp numbers are
# 10-15 digits with the country code; 8 is short enough to tolerate a missing prefix and long
# enough that an ordinary number in a message ("@2024 budget") cannot collide.
_MIN_DIGITS = 8

# What someone types when their client did not turn the mention into a number. Matched only as a
# whole word directly after an `@`, never as the word "Penny" in a sentence.
_ALIAS_RE = re.compile(r"@penny\b", re.IGNORECASE)

# Any payload key containing this is treated as "a list of mentioned people", whatever GOWA
# actually calls it — `mentioned_jid`, `mentionedJids`, `mentions`, nested under `context_info`.
# Broad on purpose: this path is a bonus signal, and the text match above is the real one.
_MENTION_KEY = "mention"

# The payload is stored verbatim and arrives from outside; a malicious or malformed one could
# nest arbitrarily deep. Bounded so a mention check can never become a stack overflow.
_MAX_DEPTH = 6

_NON_DIGITS = re.compile(r"\D")


def normalise_jid(value: str | None) -> str | None:
    """The bare digits of a JID, or None if there aren't enough of them to identify anyone.

    `447473209317:12@s.whatsapp.net`, `+1 (747) 320-9317` and `17473209317` all reduce to their
    digits. The device suffix (`:12`) and the domain are dropped first so a multi-device JID
    does not smuggle a device number into the comparison.
    """
    if not value:
        return None
    bare = value.split("@")[0].split(":")[0]
    digits = _NON_DIGITS.sub("", bare)
    return digits if len(digits) >= _MIN_DIGITS else None


def _same_number(candidate: str, own: str) -> bool:
    """Two digit strings naming the same phone.

    Equal, or one is a suffix of the other — a client may show `447473209317` where another
    shows `7473209317`, and from here there is no way to know which form the sender's app used.
    Both are already at least `_MIN_DIGITS` long, so the suffix rule cannot be satisfied by a
    stray short number.
    """
    if len(candidate) < _MIN_DIGITS:
        return False
    return candidate == own or candidate.endswith(own) or own.endswith(candidate)


def _text_mentions(text: str, own: str) -> bool:
    """Any `@<number>` in the body that resolves to our number."""
    return any(
        _same_number(_NON_DIGITS.sub("", match.group(1)), own)
        for match in _MENTION_RE.finditer(text)
    )


def _iter_mention_strings(value: Any, *, under_mention_key: bool, depth: int = 0) -> list[str]:
    """Every string sitting under a key that looks like a mention list.

    Walks the raw payload rather than reading one hard-coded path, because the shape GOWA uses
    for mentions is unverified — `mentioned_jid` at the top level and `contextInfo.mentionedJid`
    are both plausible and only one of them can be guessed right.
    """
    if depth > _MAX_DEPTH:
        return []
    if isinstance(value, str):
        return [value] if under_mention_key else []
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found += _iter_mention_strings(
                item, under_mention_key=under_mention_key, depth=depth + 1
            )
        return found
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            keyed = under_mention_key or (isinstance(key, str) and _MENTION_KEY in key.lower())
            found += _iter_mention_strings(item, under_mention_key=keyed, depth=depth + 1)
        return found
    return []


def _payload_mentions(payload: dict[str, Any], own: str) -> bool:
    """The metadata path. Present or absent, correct or missing — never load-bearing alone."""
    return any(
        _same_number(digits, own)
        for raw in _iter_mention_strings(payload, under_mention_key=False)
        if (digits := _NON_DIGITS.sub("", raw.split("@")[0].split(":")[0]))
    )


def has_mention_marker(text: str | None, payload: dict[str, Any] | None) -> bool:
    """A CHEAP pre-filter: could this message conceivably be a mention?

    Pure string work, no I/O, deliberately over-inclusive. It exists so the webhook can skip
    resolving the paired number — a call to the GOWA sidecar — for the overwhelming majority of
    messages, which contain no `@` at all. Anything it lets through is decided properly by
    `mentions_penny`; anything it rejects could not have matched there either, because every
    rule in this module requires an `@` or a mention-keyed payload field.
    """
    if text and "@" in text:
        return True
    if not payload:
        return False
    return bool(_iter_mention_strings(payload, under_mention_key=False))


def mentions_penny(text: str | None, payload: dict[str, Any], own_jid: str | None) -> bool:
    """Was Penny explicitly addressed in this message?

    `own_jid` is the paired account's JID, from `gowa.list_devices()`. It may be None — the
    sidecar can be down or unpaired — and that is not a reason to fall silent entirely, so the
    literal `@penny` alias is still honoured. It is never a reason to answer MORE: with no
    number to compare against, no number can match.
    """
    body = text or ""
    if body and _ALIAS_RE.search(body):
        return True
    own = normalise_jid(own_jid)
    if own is None:
        return False
    if body and _text_mentions(body, own):
        return True
    return _payload_mentions(payload or {}, own)
