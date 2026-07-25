"""The dedup key, and the pure decision of what to do when one collides.

    dedup_key = "llm:" + sha256(f"{kind}|{bucket}|{norm(natural_key)}").hexdigest()
    dedup_key = f"human:{event_id}"          # UI-authored, never merged

WE COMPUTE THE KEY, NEVER THE MODEL. Models are inconsistent hashers: the same event
described twice yields two keys, so re-extraction would duplicate everything and the
`(household_id, dedup_key)` unique index would protect nothing. The model supplies
`natural_key` — a short human phrase like "dr aziz gp" — and we do the arithmetic.

The key is DELIBERATELY COARSE. Showing the same cardiology appointment five times is
the exact pain Penny exists to remove; merging two GP visits that happened on one day is
a smaller wound, and the UI ships a Split action for it. When in doubt, over-merge.

Pure by construction: no IO, no clock. Everything date-shaped is resolved in the
HOUSEHOLD timezone, because a care history is a local-clock story.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha1, sha256
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from uuid import UUID
    from zoneinfo import ZoneInfo

# "Dr Aziz", "Doctor Aziz" and "Aziz" are one provider. Stripping the honorific is worth
# more than it costs: the failure it prevents (two cards for one appointment) is visible
# on every feed, the failure it causes (a nun named Sister Agnes) is not in this domain.
_HONORIFICS = re.compile(r"\b(dr|doctor|mr|mrs|ms|prof|professor|nurse|sister)\b\.?", re.IGNORECASE)

# A dose change is not a daily event: "upped her amlodipine" said on Monday and again on
# Thursday is one change, so these actions bucket by month. The rest are episodic and
# genuinely happen per-day — two missed doses in a week are two facts.
_MONTHLY_MEDICATION_ACTIONS = frozenset({"started", "stopped", "changed"})


class ExtractedEventLike(Protocol):
    """Exactly the fields dedup reads off `ExtractedEvent`. The whole coupling, listed.

    Structural rather than an import so this module stays pure stdlib and testable
    without the LLM schemas — and so a rename over there fails here loudly.
    """

    kind: str
    natural_key: str
    title: str
    occurred_at: str | None  # ISO 8601; the model is told the household timezone
    occurred_at_precision: str
    medication_action: str | None
    dose_text: str | None
    outcome: str | None
    follow_up_actions: list[str]
    attendees: list[str]


class MergeDecision(StrEnum):
    """What the runner should do with an incoming event whose key already exists."""

    NO_CHANGE = "no_change"
    APPEND_SOURCES = "append_sources"
    NEEDS_LLM_MERGE = "needs_llm_merge"


def normalise(value: str | None) -> str:
    """Fold a phrase to a stable slug: NFKD to ASCII, honorifics out, hyphens between."""
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    folded = _HONORIFICS.sub(" ", folded.casefold())
    return "-".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def compute_dedup_key(event: ExtractedEventLike, tz: ZoneInfo) -> str:
    """The key two extractions of the same real-world event must agree on."""
    # An empty natural_key would collapse every event of this kind in the bucket into
    # one row, which is the one over-merge the product cannot explain to a family.
    natural = normalise(event.natural_key) or normalise(event.title) or "unkeyed"
    payload = f"{event.kind}|{_bucket(event, tz)}|{natural}"
    return "llm:" + sha256(payload.encode()).hexdigest()


def human_dedup_key(event_id: UUID) -> str:
    """A hand-written event keys on itself, so extraction can never merge into it."""
    return f"human:{event_id}"


def decide_merge(existing: dict[str, Any], incoming: ExtractedEventLike) -> MergeDecision:
    """Decide, without calling anything, whether a collision needs a paid merge.

    `existing` is the stored event in its API shape — `kind` and `edited_at` at the top
    level, kind-specific fields under `details` — so this reads the same row the UI does.

    Most collisions are a family mentioning the same thing twice and cost nothing to
    resolve. Only an appointment or medication that GAINS information is worth a call.
    """
    # A human edit is permanent. This mirrors the upsert's `WHERE events.edited_at IS
    # NULL`: one policy, not two, or the SQL and this function drift apart silently.
    if existing.get("edited_at"):
        return MergeDecision.NO_CHANGE

    kind = existing["kind"]
    if kind in ("symptom", "note"):
        # A recurring symptom is a count, not a rewrite: three mentions of a bad night
        # add three sources to one event and there is nothing for a model to reconcile.
        return MergeDecision.APPEND_SOURCES

    details = existing.get("details") or {}
    if kind == "appointment" and _appointment_gains(details, incoming):
        return MergeDecision.NEEDS_LLM_MERGE
    if kind == "medication" and _dose_changed(details, incoming):
        return MergeDecision.NEEDS_LLM_MERGE
    return MergeDecision.APPEND_SOURCES


def _appointment_gains(details: dict[str, Any], incoming: ExtractedEventLike) -> bool:
    """An appointment is one event for its whole life; this is it turning into an outcome."""
    return bool(
        (incoming.outcome and not details.get("outcome"))
        or (incoming.follow_up_actions and not details.get("follow_up_actions"))
        or (incoming.attendees and not details.get("attendees"))
    )


def _dose_changed(details: dict[str, Any], incoming: ExtractedEventLike) -> bool:
    dose = _dose_signature(incoming.dose_text)
    return bool(dose) and dose != _dose_signature(details.get("dose_text"))


def _dose_signature(dose: str | None) -> str:
    # Spacing carries no meaning in a dose: "5mg" and "5 mg" are the same prescription,
    # and paying for a merge call every time someone retypes it with a space is waste.
    return normalise(dose).replace("-", "")


def _bucket(event: ExtractedEventLike, tz: ZoneInfo) -> str:
    day = _local_date(event.occurred_at, tz)
    if day is None:
        # Without this suffix every undated event in the household — a symptom with no
        # date, a note about the blue badge — collapses into a single row.
        return f"undated:{sha1(normalise(event.title).encode()).hexdigest()[:8]}"
    grain = _grain(event)
    if grain == "month":
        return day.strftime("%Y-%m")
    if grain == "week":
        return day.strftime("%G-W%V")
    return day.isoformat()


def _grain(event: ExtractedEventLike) -> str:
    precision = event.occurred_at_precision
    monthly_medication = (
        event.kind == "medication" and event.medication_action in _MONTHLY_MEDICATION_ACTIONS
    )
    if precision == "month" or monthly_medication:
        return "month"
    if precision == "week":
        return "week"
    return "day"


def _local_date(occurred_at: str | None, tz: ZoneInfo) -> date | None:
    if not occurred_at:
        return None
    text = occurred_at.strip().replace("Z", "+00:00")
    if len(text) == 7 and text[4] == "-":
        text = f"{text}-01"  # "2026-08" from a month-precision answer
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # The model is told the household timezone and answers in local wall clock, so a
        # naive value is ALREADY local; converting it would shift the day.
        return moment.date()
    # And an absolute instant must be converted, or 23:40 on the 14th in UTC files itself
    # under the 15th — a day the family never mentioned.
    return moment.astimezone(tz).date()
