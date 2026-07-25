"""The dedup key is what stops the same appointment appearing five times, and what
occasionally merges two things that were not the same. Both halves are pinned here.

The KNOWN FAILURE MODES are written as passing tests on purpose: the key is deliberately
coarse, so every one of these behaviours is a decision. If someone changes the key, these
fail and they have to decide again rather than discover it on a family's feed.
"""

from dataclasses import dataclass, field, replace
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.extraction.dedup import (
    MergeDecision,
    compute_dedup_key,
    decide_merge,
    human_dedup_key,
    normalise,
)

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


@dataclass
class FakeExtractedEvent:
    """Stands in for app.llm.schemas.ExtractedEvent — dedup only reads these fields."""

    kind: str = "symptom"
    natural_key: str = "bad night"
    title: str = "Poor sleep"
    occurred_at: str | None = "2026-07-14"
    occurred_at_precision: str = "day"
    medication_action: str | None = None
    dose_text: str | None = None
    outcome: str | None = None
    follow_up_actions: list[str] = field(default_factory=list)
    attendees: list[str] = field(default_factory=list)


def key(kind: str, bucket: str, natural: str) -> str:
    return "llm:" + sha256(f"{kind}|{bucket}|{natural}".encode()).hexdigest()


def stored(kind: str, **details) -> dict:
    """A stored event in its API shape: kind and edited_at on top, the rest in details."""
    return {"kind": kind, "edited_at": None, "details": details}


def test_the_key_is_exactly_the_documented_formula() -> None:
    # Every dedup_key in every household depends on this string; pin it literally.
    event = FakeExtractedEvent(
        kind="appointment", natural_key="Dr Aziz GP", occurred_at="2026-07-17"
    )
    assert compute_dedup_key(event, LONDON) == key("appointment", "2026-07-17", "aziz-gp")


def test_human_authored_events_key_on_themselves_and_never_merge() -> None:
    event_id = uuid4()
    assert human_dedup_key(event_id) == f"human:{event_id}"
    assert not human_dedup_key(event_id).startswith("llm:")


def test_normalise_folds_honorifics_accents_and_punctuation() -> None:
    assert normalise("Dr. Aziz") == normalise("Doctor Aziz") == normalise("aziz") == "aziz"
    assert normalise("Beech Rd. Surgery") == "beech-rd-surgery"
    assert normalise("Dr Müller") == "muller"
    assert normalise(None) == ""


def test_honorific_drift_does_not_split_one_appointment() -> None:
    formal = FakeExtractedEvent(
        kind="appointment", natural_key="Dr. Aziz", occurred_at="2026-07-17"
    )
    casual = replace(formal, natural_key="aziz")
    assert compute_dedup_key(formal, LONDON) == compute_dedup_key(casual, LONDON)


def test_buckets_are_computed_in_the_household_timezone_never_utc() -> None:
    # 23:40 UTC is already tomorrow in London and still yesterday in New York. Bucketing
    # in UTC files the event under a day the family never mentioned.
    late = FakeExtractedEvent(occurred_at="2026-07-14T23:40:00Z")
    assert compute_dedup_key(late, LONDON) == key("symptom", "2026-07-15", "bad-night")
    assert compute_dedup_key(late, NEW_YORK) == key("symptom", "2026-07-14", "bad-night")


def test_a_naive_answer_is_already_local_and_is_not_shifted() -> None:
    # The model is told the household timezone, so it answers in local wall clock.
    naive = FakeExtractedEvent(occurred_at="2026-07-14T23:40")
    assert compute_dedup_key(naive, LONDON) == key("symptom", "2026-07-14", "bad-night")


def test_month_and_week_precision_widen_the_bucket() -> None:
    monthly = FakeExtractedEvent(occurred_at="2026-07-14", occurred_at_precision="month")
    weekly = FakeExtractedEvent(occurred_at="2026-07-14", occurred_at_precision="week")
    assert compute_dedup_key(monthly, LONDON) == key("symptom", "2026-07", "bad-night")
    assert compute_dedup_key(weekly, LONDON) == key("symptom", "2026-W29", "bad-night")


def test_a_bare_year_month_answer_parses() -> None:
    event = FakeExtractedEvent(occurred_at="2026-08", occurred_at_precision="month")
    assert compute_dedup_key(event, LONDON) == key("symptom", "2026-08", "bad-night")


def test_an_unparseable_date_falls_back_to_undated() -> None:
    unparseable = FakeExtractedEvent(occurred_at="next Tuesday")
    undated = FakeExtractedEvent(occurred_at=None)
    assert compute_dedup_key(unparseable, LONDON) == compute_dedup_key(undated, LONDON)


def test_a_dose_change_buckets_by_month_and_a_missed_dose_by_day() -> None:
    # "Upped her amlodipine" on the 14th and again on the 28th is one change, not two.
    started = FakeExtractedEvent(
        kind="medication", natural_key="amlodipine", medication_action="started"
    )
    assert compute_dedup_key(started, LONDON) == compute_dedup_key(
        replace(started, occurred_at="2026-07-28"), LONDON
    )

    missed = replace(started, medication_action="missed")
    assert compute_dedup_key(missed, LONDON) != compute_dedup_key(
        replace(missed, occurred_at="2026-07-28"), LONDON
    )


def test_an_empty_natural_key_falls_back_to_the_title() -> None:
    # Without the fallback every event of one kind in a bucket collapses into one row.
    dizzy = FakeExtractedEvent(natural_key="", title="Dizzy on standing")
    ankles = FakeExtractedEvent(natural_key="", title="Swollen ankles")
    assert compute_dedup_key(dizzy, LONDON) != compute_dedup_key(ankles, LONDON)


# --- known failure modes, accepted, each with the mitigation the product ships ---------


def test_two_gp_appointments_same_day_collapse() -> None:
    """ACCEPTED. Two separate GP visits on one day merge into a single event.

    Over-merging beats duplicate cards: showing one appointment five times is the exact
    pain Penny exists to remove, while a merged pair is one card the family can see is
    wrong. The mitigation is the UI's Split action, which forks the second visit onto a
    pinned sibling key so re-extraction never re-merges it.
    """
    morning = FakeExtractedEvent(
        kind="appointment", natural_key="Dr Aziz", occurred_at="2026-07-17T09:30"
    )
    afternoon = replace(morning, occurred_at="2026-07-17T16:00", title="Second GP visit")
    assert compute_dedup_key(morning, LONDON) == compute_dedup_key(afternoon, LONDON)


def test_provider_spelling_drift_produces_two_keys() -> None:
    """ACCEPTED. "Dr Aziz" and "the surgery" are one appointment described twice.

    No normalisation reaches this: the phrases share no characters. The mitigation is the
    weekly dedup-candidate sweep, which pairs appointments within +/-2 days in the same
    household and asks the model once — roughly ten pairs and $0.02 per household a week.
    """
    named = FakeExtractedEvent(kind="appointment", natural_key="Dr Aziz", occurred_at="2026-07-17")
    vague = replace(named, natural_key="the surgery")
    assert compute_dedup_key(named, LONDON) != compute_dedup_key(vague, LONDON)


def test_reschedule_tuesday_to_thursday_is_two_events() -> None:
    """BY DESIGN, and not a failure to fix. A moved appointment gets a second key.

    Collapsing them would erase the fact that it moved, which is care-relevant history.
    The merge pass records supersedes_dedup_key on the abandoned Tuesday and the feed
    renders it struck through rather than deleting it.
    """
    tuesday = FakeExtractedEvent(
        kind="appointment", natural_key="Dr Aziz", occurred_at="2026-07-14"
    )
    thursday = replace(tuesday, occurred_at="2026-07-16")
    assert compute_dedup_key(tuesday, LONDON) != compute_dedup_key(thursday, LONDON)


def test_precision_mismatch_month_vs_day_duplicates() -> None:
    """ACCEPTED, low frequency. "Sometime next month" and "the 17th" are one appointment
    but land in a month bucket and a day bucket, so they duplicate.

    Widening every key to the coarsest precision seen would merge a whole month of GP
    visits, which is far worse. The same weekly dedup-candidate sweep catches this pair.
    """
    vague = FakeExtractedEvent(
        kind="appointment",
        natural_key="Dr Aziz",
        occurred_at="2026-07-01",
        occurred_at_precision="month",
    )
    exact = replace(vague, occurred_at="2026-07-17", occurred_at_precision="day")
    assert compute_dedup_key(vague, LONDON) != compute_dedup_key(exact, LONDON)


def test_undated_events_do_not_all_collapse() -> None:
    """MITIGATED IN THE KEY ITSELF. Undated events share the bucket "undated", so without
    a discriminator every dateless note in the household would merge into one row.

    The bucket therefore carries sha1(norm(title))[:8]. These two notes share a
    natural_key and differ only in title, and that is enough to keep them apart.
    """
    walking_frame = FakeExtractedEvent(
        kind="note", natural_key="equipment", title="Walking frame ordered", occurred_at=None
    )
    blue_badge = replace(walking_frame, title="Blue badge renewal due")
    assert compute_dedup_key(walking_frame, LONDON) != compute_dedup_key(blue_badge, LONDON)
    assert compute_dedup_key(walking_frame, LONDON) == compute_dedup_key(
        replace(walking_frame), LONDON
    )


# --- merge rules -----------------------------------------------------------------------


def test_symptoms_and_notes_never_cost_a_merge_call() -> None:
    # A recurring symptom is a count, not a rewrite: three bad nights are three sources.
    for kind in ("symptom", "note"):
        decision = decide_merge(stored(kind), FakeExtractedEvent(kind=kind))
        assert decision is MergeDecision.APPEND_SOURCES


def test_an_appointment_gaining_an_outcome_needs_the_model() -> None:
    existing = stored("appointment", outcome=None, follow_up_actions=[], attendees=[])
    incoming = FakeExtractedEvent(kind="appointment", outcome="Bloods taken, review in 2 weeks")
    assert decide_merge(existing, incoming) is MergeDecision.NEEDS_LLM_MERGE


def test_follow_ups_and_attendees_each_earn_a_merge_of_their_own() -> None:
    existing = stored("appointment", outcome="Bloods taken", follow_up_actions=[], attendees=[])
    follow_up = FakeExtractedEvent(kind="appointment", follow_up_actions=["Book review 31 July"])
    attendee = FakeExtractedEvent(kind="appointment", attendees=["Sarah"])
    assert decide_merge(existing, follow_up) is MergeDecision.NEEDS_LLM_MERGE
    assert decide_merge(existing, attendee) is MergeDecision.NEEDS_LLM_MERGE


def test_an_appointment_mentioned_again_with_nothing_new_is_free() -> None:
    existing = stored(
        "appointment",
        outcome="Bloods taken",
        follow_up_actions=["Book review 31 July"],
        attendees=["Sarah"],
    )
    incoming = FakeExtractedEvent(
        kind="appointment",
        outcome="Bloods taken",
        follow_up_actions=["Book review"],
        attendees=["Sarah", "Margaret"],
    )
    assert decide_merge(existing, incoming) is MergeDecision.APPEND_SOURCES


def test_a_new_or_changed_dose_needs_the_model() -> None:
    known = stored("medication", medication_name="amlodipine", dose_text="5mg")
    unknown = stored("medication", medication_name="amlodipine")
    changed = FakeExtractedEvent(kind="medication", dose_text="10 mg")
    assert decide_merge(known, changed) is MergeDecision.NEEDS_LLM_MERGE
    assert decide_merge(unknown, changed) is MergeDecision.NEEDS_LLM_MERGE


def test_the_same_dose_written_differently_is_free() -> None:
    existing = stored("medication", medication_name="amlodipine", dose_text="5mg")
    restated = FakeExtractedEvent(kind="medication", dose_text="5 MG")
    unmentioned = FakeExtractedEvent(kind="medication", dose_text=None)
    assert decide_merge(existing, restated) is MergeDecision.APPEND_SOURCES
    assert decide_merge(existing, unmentioned) is MergeDecision.APPEND_SOURCES


def test_a_human_edit_is_permanent() -> None:
    """One policy, not two: this mirrors the upsert's `WHERE events.edited_at IS NULL`,
    so re-extraction cannot quietly overwrite what a family member corrected by hand."""
    edited = {"kind": "appointment", "edited_at": "2026-07-18T10:00:00Z", "details": {}}
    incoming = FakeExtractedEvent(kind="appointment", outcome="Bloods taken")
    assert decide_merge(edited, incoming) is MergeDecision.NO_CHANGE
