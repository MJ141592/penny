"""Generate `fixtures/demo_family.txt` and its labelled ground truth, byte-reproducibly.

The fixture is the M1 go/no-go artefact: extraction is scored against the 24 labelled events
in `demo_family_events.json`, so the transcript and the labels must never be able to drift.
They can't, because both are derived from the SAME literal below — `SCRIPT` carries every
care-relevant message and tags it with the ground-truth event it belongs to.

Two halves, deliberately:

  * `SCRIPT` — hand-written. ~170 messages carrying exactly 24 events plus five distractors
    that LOOK like care events and are not. Hand-written because the whole point is the mess:
    a cancelled-then-rebooked appointment, a symptom recurring three times, two siblings
    reporting one fall an hour apart, relative-only dates, threads split across chunks.
  * `FILLER` — generated from templates with a fixed seed. ~1,800 messages of ordinary family
    chatter. Filler must be genuinely non-extractable, which is enforced, not hoped for:
    `_assert_filler_is_inert` rejects any generated line containing a care word.

Run it twice, diff the output, get nothing. Determinism comes from `random.Random(SEED)` and
from writing with an explicit `\\n` newline and utf-8 encoding.

    uv run python -m scripts.make_demo_fixture

Format is an Android WhatsApp export, `dd/mm/yyyy, HH:MM - Sender: text`, Europe/London wall
clock, dayfirst. Continuation lines, `<Media omitted>` placeholders and system lines are all
present because the parser has to survive them.
"""

# Verbatim WhatsApp message text is data, not code: wrapping it to 100 columns would make the
# fixture unreadable and invite silent edits to the exact strings the ground truth points at.
# ruff: noqa: E501

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Not arbitrary: this is the seed whose filler density puts a chunk break inside BOTH threads
# that are labelled as straddling one (14-15 April and 18-19 June). `_verify` re-checks it, so
# changing the seed or the filler volume fails loudly rather than silently weakening the eval.
SEED = 20260763
TZ = ZoneInfo("Europe/London")
START = date(2026, 1, 12)
END = date(2026, 7, 12)
TARGET_FILLER = 1800

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TXT_PATH = FIXTURES / "demo_family.txt"
JSON_PATH = FIXTURES / "demo_family_events.json"

# The chunker Track A builds (appendix §3). Simulated here only to prove that the 14-15 April
# thread really does straddle a chunk boundary — a claim the ground truth makes in writing.
CHUNK_MIN_FOR_GAP_BREAK = 80
CHUNK_HARD_MAX = 200
CHUNK_QUIET_GAP = timedelta(hours=4)
CHUNK_MAX_SPAN = timedelta(days=14)


# --------------------------------------------------------------------------------------
# Ground truth. `occurred_at` is a LOCAL wall-clock spec; precision decides how it is read:
#   exact -> "YYYY-MM-DD HH:MM" converted to UTC
#   day   -> "YYYY-MM-DD"  emitted as that date at 00:00:00Z (the time is meaningless)
#   week  -> the Monday of the ISO week, 00:00:00Z
#   month -> "YYYY-MM", the 1st at 00:00:00Z
# Week/month never go through a timezone conversion on purpose: converting midnight local
# would push the bucket into the previous week/month during BST and corrupt the label.
# --------------------------------------------------------------------------------------

EVENTS: list[dict] = [
    {
        "id": "gt-01",
        "kind": "symptom",
        "occurred_at": "2026-01-19",
        "occurred_at_precision": "day",
        "title": "Dizzy spell standing up in the kitchen",
        "natural_key_hint": "dizziness",
        "must_mention": ["dizz"],
        "notes": "Recurrence 1 of 3. Must NOT merge with gt-07 or gt-11 — same symptom, weeks apart, three separate events. Dot re-reports it three hours later, so the same episode arrives twice from two senders on one day and must stay one event.",
    },
    {
        "id": "gt-02",
        "kind": "medication",
        "occurred_at": "2026-01-22",
        "occurred_at_precision": "day",
        "title": "Missed two doses of apixaban",
        "natural_key_hint": "apixaban-missed",
        "must_mention": ["apixaban"],
        "notes": "Two missed doses ('yesterday and again this morning') collapse to one medication event on the day they were found. The follow-on message about a dosette box is a plan, not a second event.",
    },
    {
        "id": "gt-03",
        "kind": "appointment",
        "occurred_at": "2026-01-29 11:20",
        "occurred_at_precision": "exact",
        "title": "Telephone appointment with Dr Aziz",
        "natural_key_hint": "dr-aziz-telephone",
        "must_mention": ["aziz"],
        "notes": "Booked on the 26th for 'Thursday at 11.20', the date confirmed one message later. Nobody ever reports back, so this event legitimately never gains an outcome and must stay status=scheduled.",
    },
    {
        "id": "gt-04",
        "kind": "note",
        "occurred_at": "2026-02-02",
        "occurred_at_precision": "day",
        "title": "Carer morning visit moved to 9.30",
        "natural_key_hint": "carer-visit-time-change",
        "must_mention": ["visit"],
        "notes": "Logistics note, not an appointment. Announced on 28 January but occurs 'from Monday the 2nd' — the date has to come from the text, not from the message timestamp. The word 'carer' is never used; the agency is named instead (Bright Care), which is why must_mention only pins 'visit'.",
    },
    {
        "id": "gt-05",
        "kind": "appointment",
        "occurred_at": "2026-02-24 14:15",
        "occurred_at_precision": "exact",
        "title": "Cardiology outpatients, Salford Royal (cancelled)",
        "natural_key_hint": "cardiology-salford-royal",
        "must_mention": ["cardiolog", "cancel"],
        "notes": "HAZARD: cancelled then rebooked. This is the cancelled one and must survive as its own event with status=cancelled — not deleted, not merged into gt-10, and not a duplicate of it. Booking and cancellation are 13 days apart, so they land in different chunks.",
    },
    {
        "id": "gt-06",
        "kind": "symptom",
        "occurred_at": "2026-02-10",
        "occurred_at_precision": "day",
        "title": "Fall in the hall, bruised hip",
        "natural_key_hint": "fall-hall",
        "must_mention": ["fall"],
        "notes": "HAZARD: relative-only date. Reported Thursday 12 February as 'last Tuesday' and never as a number, so occurred_at must resolve to 10 February against the message timestamp, not the extraction time. Distinct from gt-19 (different fall, three months later).",
    },
    {
        "id": "gt-07",
        "kind": "symptom",
        "occurred_at": "2026-02-16",
        "occurred_at_precision": "day",
        "title": "Dizzy spell at the kitchen sink",
        "natural_key_hint": "dizziness",
        "must_mention": ["dizz"],
        "notes": "Recurrence 2 of 3. Tom's reply, 'Same as last month then', is a backward reference and must not create a second event or re-date this one.",
    },
    {
        "id": "gt-08",
        "kind": "medication",
        "occurred_at": "2026-02-21",
        "occurred_at_precision": "day",
        "title": "Started co-codamol for hip pain",
        "natural_key_hint": "co-codamol-started",
        "must_mention": ["co-codamol"],
        "notes": "Deliberately phrased as a repeat prescription arriving, so there is no GP visit to invent. Extracting an appointment here is a false positive.",
    },
    {
        "id": "gt-09",
        "kind": "appointment",
        "occurred_at": "2026-03-16",
        "occurred_at_precision": "week",
        "title": "Physio home visit for the hip",
        "natural_key_hint": "physio-home-visit",
        "must_mention": ["physio"],
        "notes": "HAZARD: week precision. Said on Friday 6 March as 'the week after next' with the day explicitly unconfirmed, so the only correct answer is the week beginning Monday 16 March. An exact date here is a wrong answer, not a better one.",
    },
    {
        "id": "gt-10",
        "kind": "appointment",
        "occurred_at": "2026-03-17 11:00",
        "occurred_at_precision": "exact",
        "title": "Cardiology outpatients, Salford Royal",
        "natural_key_hint": "cardiology-salford-royal",
        "must_mention": ["cardiolog"],
        "notes": "HAZARD: the rebooking of gt-05, and Sarah says so out loud ('the one they cancelled in February has moved to the 17th'). Two events, not one. Also gains an outcome the same day it happens (ECG, no changes), which must merge onto this row rather than create a third.",
    },
    {
        "id": "gt-11",
        "kind": "symptom",
        "occurred_at": "2026-03-12",
        "occurred_at_precision": "day",
        "title": "Dizzy spell in the bathroom",
        "natural_key_hint": "dizziness",
        "must_mention": ["dizz"],
        "notes": "Recurrence 3 of 3. Sarah writes 'That's three now', which tempts a model into emitting one summarising event instead of the third instance.",
    },
    {
        "id": "gt-12",
        "kind": "note",
        "occurred_at": "2026-03-26",
        "occurred_at_precision": "day",
        "title": "Grab rails fitted in the bathroom",
        "natural_key_hint": "grab-rails-fitted",
        "must_mention": ["grab rail"],
        "notes": "Equipment note. A photo (<Media omitted>) sits immediately after it with no caption and must contribute nothing of its own.",
    },
    {
        "id": "gt-13",
        "kind": "symptom",
        "occurred_at": "2026-04-02",
        "occurred_at_precision": "day",
        "title": "Swollen ankles in the evening",
        "natural_key_hint": "swollen-ankles",
        "must_mention": ["ankle"],
        "notes": "Resolves by the next morning; the 'ankles look normal this morning' follow-up is part of the same event, not a second one.",
    },
    {
        "id": "gt-14",
        "kind": "appointment",
        "occurred_at": "2026-04-08 08:50",
        "occurred_at_precision": "exact",
        "title": "Fasting blood test at Brookfield surgery",
        "natural_key_hint": "blood-test-brookfield",
        "must_mention": ["blood"],
        "notes": "Booked on the 6th for 'Wednesday at 8.50' and reported done on the 8th. One event that gains status=attended; two events is a duplicate.",
    },
    {
        "id": "gt-15",
        "kind": "symptom",
        "occurred_at": "2026-04-14",
        "occurred_at_precision": "day",
        "title": "Confusion in the evening, gas ring left on",
        "natural_key_hint": "confusion-episode",
        "must_mention": ["gas"],
        "notes": "HAZARD: thread spans a chunk boundary. It opens at 22:48 on the 14th and continues at 07:12 on the 15th, across the overnight quiet gap the chunker closes chunks on, so the two halves are extracted separately and must merge. Reported second-hand via the neighbour, so the actor is Jean, not Sarah.",
    },
    {
        "id": "gt-16",
        "kind": "note",
        "occurred_at": "2026-04-20",
        "occurred_at_precision": "day",
        "title": "Key safe fitted by the back door",
        "natural_key_hint": "key-safe-fitted",
        "must_mention": ["key safe"],
        "notes": "HAZARD: the second event in the chunk-straddling 14-15 April thread. Decided on the morning side of the boundary and only actually done on the 20th, so occurred_at is the 20th while the decision sits in the previous chunk.",
    },
    {
        "id": "gt-17",
        "kind": "appointment",
        "occurred_at": "2026-05",
        "occurred_at_precision": "month",
        "title": "Podiatry appointment to be sent for May",
        "natural_key_hint": "podiatry",
        "must_mention": ["podiatry"],
        "notes": "HAZARD: month precision. 'Sometime next month' said on 21 April, with the letter still to come. Anything finer than the month of May is invented.",
    },
    {
        "id": "gt-18",
        "kind": "medication",
        "occurred_at": "2026-04-28",
        "occurred_at_precision": "day",
        "title": "Apixaban repeat prescription reordered",
        "natural_key_hint": "apixaban-refilled",
        "must_mention": ["apixaban"],
        "notes": "Same drug as gt-02 but a different action (refilled, not missed) three months later, so it must not collapse into it.",
    },
    {
        "id": "gt-19",
        "kind": "symptom",
        "occurred_at": "2026-05-09",
        "occurred_at_precision": "day",
        "title": "Fall in the bathroom, bruised arm",
        "natural_key_hint": "fall-bathroom",
        "must_mention": ["fall"],
        "notes": "HAZARD: two siblings report the same fall 39 minutes apart, independently — Sarah at 18:12 having been round, Tom at 18:51 having spoken to Margaret on the phone. Wording differs ('a fall' vs 'a tumble'). One event, not two.",
    },
    {
        "id": "gt-20",
        "kind": "note",
        "occurred_at": "2026-05-20",
        "occurred_at_precision": "day",
        "title": "Referral to the memory service submitted",
        "natural_key_hint": "memory-service-referral",
        "must_mention": ["memory"],
        "notes": "Admin note, not an appointment: nothing is booked and there is no date, only a three-month wait. Emitting an appointment for the referral is a false positive.",
    },
    {
        "id": "gt-21",
        "kind": "appointment",
        "occurred_at": "2026-06-02 15:40",
        "occurred_at_precision": "exact",
        "title": "Hip X-ray at Salford Royal (missed)",
        "natural_key_hint": "hip-xray-salford-royal",
        "must_mention": ["x-ray"],
        "notes": "Booked on 27 May, then missed — Margaret was at the lunch club. One event that ends status=missed, with Tom as the attendee who turned up. Never rebooked in the transcript, so no follow-up event exists to find.",
    },
    {
        "id": "gt-22",
        "kind": "appointment",
        "occurred_at": "2026-06-17 09:30",
        "occurred_at_precision": "exact",
        "title": "GP appointment with Dr Aziz, Brookfield surgery",
        "natural_key_hint": "dr-aziz-brookfield",
        "must_mention": ["aziz"],
        "notes": "HAZARD: discussed before (9 June, and a multi-line list of questions on the 16th) and reported on afterwards in a separate thread on the evening of the 18th, a day after it happened. ONE appointment event that gains details.outcome, follow_up_actions and status=attended — there is no appointment_outcome kind and no parent_event_id. The 18-19 June thread also crosses an overnight gap.",
    },
    {
        "id": "gt-23",
        "kind": "medication",
        "occurred_at": "2026-06-17",
        "occurred_at_precision": "day",
        "title": "Bisoprolol halved to 2.5mg",
        "natural_key_hint": "bisoprolol-changed",
        "must_mention": ["bisoprolol"],
        "notes": "Reported on the 18th but anchored by 'at the appointment', so it belongs to the 17th. A separate medication event from gt-22 even though one message carries both.",
    },
    {
        "id": "gt-24",
        "kind": "appointment",
        "occurred_at": "2026-08-06 10:15",
        "occurred_at_precision": "exact",
        "title": "Cardiology follow-up, Salford Royal",
        "natural_key_hint": "cardiology-salford-royal",
        "must_mention": ["cardiolog"],
        "notes": "The only event dated after the export ends, so it is the one that proves /api/upcoming works. Same clinic as gt-05 and gt-10 but five months later, which is exactly what a too-coarse dedup key would over-merge.",
    },
]


# --------------------------------------------------------------------------------------
# The hand-written care thread. (local "YYYY-MM-DD HH:MM", sender, text, tag)
# tag is "" for in-thread chatter that carries no event, "gt-NN" (comma-separated when one
# message carries two events), or "trap:<slug>" for a message that LOOKS like a care event
# and is not — extracting one of those counts as a false positive.
# --------------------------------------------------------------------------------------

SCRIPT: list[tuple[str, str, str, str]] = [
    (
        "2026-01-15 16:20",
        "Sarah",
        "Took Mum to the garden centre this afternoon. She had a scone and told me the price of everything.",
        "",
    ),
    ("2026-01-15 16:31", "Priya", "😂 sounds about right", ""),
    (
        "2026-01-19 09:12",
        "Sarah",
        "Mum rang me in a right state first thing. She went really dizzy standing up from the kitchen table and had to hang on to the worktop.",
        "gt-01",
    ),
    (
        "2026-01-19 09:14",
        "Sarah",
        "She says it passed after a minute or two and she's fine now.",
        "gt-01",
    ),
    (
        "2026-01-19 09:31",
        "Tom",
        "Was she up in the night? She might just be getting up too quick.",
        "",
    ),
    (
        "2026-01-19 09:40",
        "Sarah",
        "Maybe. I've told her to sit on the edge of the chair for a minute before she stands.",
        "",
    ),
    (
        "2026-01-19 09:45",
        "Priya",
        "Is she drinking enough? It's been so cold, she might not be.",
        "",
    ),
    ("2026-01-19 09:50", "Sarah", "Probably not. I'll take some squash round.", ""),
    (
        "2026-01-19 12:05",
        "Dot",
        "our margaret told me about the dizzy do this morning when i rang, she sounded alright by then",
        "gt-01",
    ),
    ("2026-01-19 12:22", "Tom", "Thanks Dot", ""),
    (
        "2026-01-22 18:40",
        "Sarah",
        "Been through Mum's blister pack with her tonight. She's missed the apixaban yesterday and again this morning, they're both still sat in there.",
        "gt-02",
    ),
    ("2026-01-22 18:52", "Tom", "That's not great is it. Is the pack confusing her?", ""),
    (
        "2026-01-22 19:03",
        "Sarah",
        "I think she just forgets whether she's had her breakfast. I'm going to ring the pharmacy about one of those boxes with the days written on.",
        "gt-02",
    ),
    ("2026-01-22 19:20", "Priya", "Do you want me to ring them? I'm off tomorrow.", ""),
    ("2026-01-22 19:26", "Sarah", "No it's fine, I'll do it.", ""),
    (
        "2026-01-26 14:02",
        "Sarah",
        "Booked Mum a telephone appointment with Dr Aziz for Thursday at 11.20.",
        "gt-03",
    ),
    ("2026-01-26 14:05", "Tom", "This Thursday?", ""),
    (
        "2026-01-26 14:06",
        "Sarah",
        "Yes, the 29th, 11.20. He rings her landline so she needs to be sat by the phone.",
        "gt-03",
    ),
    ("2026-01-26 14:11", "Dot", "ill make sure shes not off to the shop", ""),
    (
        "2026-01-28 16:44",
        "Sarah",
        "Bright Care rang. They're moving Mum's morning visit from 8am to half nine from Monday the 2nd, it's a new rota.",
        "gt-04",
    ),
    ("2026-01-28 16:47", "Tom", "Is that better or worse for her?", ""),
    ("2026-01-28 16:49", "Sarah", "Better I think, she's never up and dressed for 8.", "gt-04"),
    ("2026-01-28 17:02", "Dot", "half nine is more civilised", ""),
    (
        "2026-01-31 11:15",
        "Sarah",
        "Mum's asking if anyone wants the big blue casserole dish, it's going in the charity bag otherwise.",
        "",
    ),
    ("2026-01-31 11:40", "Priya", "I'll have it!", ""),
    (
        "2026-02-05 11:20",
        "Sarah",
        "Letter's come for Mum. Cardiology outpatients at Salford Royal, Tuesday 24 February at 2.15.",
        "gt-05",
    ),
    (
        "2026-02-05 11:22",
        "Sarah",
        "That's the follow up for the AF. I'll book the afternoon off and take her.",
        "gt-05",
    ),
    ("2026-02-05 11:41", "Tom", "Good. Shout if you want me over for it.", ""),
    ("2026-02-05 11:45", "Priya", "Do you want me to do the school run that day?", ""),
    ("2026-02-05 11:50", "Sarah", "Yes please, that'd help.", ""),
    (
        "2026-02-08 20:11",
        "Dot",
        "our margaret was on great form tonight, we were on the phone an hour about next doors extension",
        "",
    ),
    (
        "2026-02-12 20:15",
        "Dot",
        "sarah did our margaret tell you she had a fall last tuesday, she only just mentioned it when i rang tonight",
        "gt-06",
    ),
    ("2026-02-12 20:19", "Sarah", "She did not. What happened?", ""),
    (
        "2026-02-12 20:24",
        "Dot",
        "she caught her foot on the rug in the hall and went down on her side, she got herself up and shes got a bruise on her hip thats all",
        "gt-06",
    ),
    (
        "2026-02-12 20:31",
        "Sarah",
        "I'm going round in the morning. Why does she never say anything.",
        "",
    ),
    ("2026-02-12 21:02", "Tom", "And she's only saying now. Typical Mum.", ""),
    ("2026-02-13 09:40", "Sarah", "Rug's gone in the bin. She wasn't happy about it.", ""),
    ("2026-02-13 09:45", "Tom", "Good. That rug's been a menace for years.", ""),
    (
        "2026-02-16 08:52",
        "Sarah",
        "Mum's gone dizzy again this morning, she said the kitchen went sideways on her when she stood up at the sink.",
        "gt-07",
    ),
    ("2026-02-16 09:10", "Tom", "Same as last month then.", "gt-07"),
    (
        "2026-02-16 09:12",
        "Sarah",
        "Carer was there thankfully. She's sat down with a cup of tea.",
        "",
    ),
    ("2026-02-16 19:30", "Sarah", "She's been alright the rest of the day.", ""),
    (
        "2026-02-18 09:33",
        "Sarah",
        "Salford Royal have just rung. They've cancelled Mum's cardiology appointment on the 24th, the consultant's off.",
        "gt-05",
    ),
    ("2026-02-18 09:35", "Sarah", "They said someone will ring with a new date.", "gt-05"),
    (
        "2026-02-18 09:51",
        "Tom",
        "That's annoying. She'll have got herself worked up about it for nothing.",
        "",
    ),
    ("2026-02-18 10:12", "Dot", "typical that", ""),
    ("2026-02-18 10:30", "Priya", "Shall I ring them next week if nobody's been in touch?", ""),
    (
        "2026-02-21 17:40",
        "Sarah",
        "The co-codamol Dr Aziz put on Mum's repeat has come through. She's started taking them today for the hip, one or two when she needs them.",
        "gt-08",
    ),
    ("2026-02-21 17:55", "Priya", "Do they make her drowsy? They knock some people out.", ""),
    (
        "2026-02-21 18:10",
        "Sarah",
        "We'll see. She's had two today and says the hip is easier already.",
        "gt-08",
    ),
    ("2026-02-21 18:22", "Tom", "Good.", ""),
    (
        "2026-02-25 08:30",
        "Priya",
        "I've got the dentist at 11 so I'll be off my phone for an hour.",
        "trap:priya-dentist",
    ),
    ("2026-02-25 08:33", "Sarah", "Good luck 😬", ""),
    (
        "2026-03-02 15:10",
        "Sarah",
        "Salford Royal have rung with a new cardiology date for Mum. Tuesday 17 March, 11am, same clinic.",
        "gt-10",
    ),
    (
        "2026-03-02 15:12",
        "Sarah",
        "So the one they cancelled in February has moved to the 17th.",
        "gt-10",
    ),
    ("2026-03-02 15:30", "Tom", "Put it in the family calendar.", ""),
    ("2026-03-02 15:35", "Priya", "Added it 👍", ""),
    (
        "2026-03-06 13:22",
        "Sarah",
        "Physio have rung about Mum's hip. They can see her the week after next, they'll confirm which day nearer the time.",
        "gt-09",
    ),
    ("2026-03-06 13:25", "Tom", "At the surgery or at the house?", ""),
    (
        "2026-03-06 13:27",
        "Sarah",
        "At the house, which is a relief. It's the falls team physio.",
        "gt-09",
    ),
    ("2026-03-06 13:40", "Priya", "That's good, she'll get on better at home.", ""),
    (
        "2026-03-09 21:40",
        "Sarah",
        "My head has been banging all day. I've taken two paracetamol and I'm going to bed.",
        "trap:sarah-headache",
    ),
    ("2026-03-09 21:44", "Priya", "Get some sleep, you've been running on empty for weeks.", ""),
    (
        "2026-03-12 07:58",
        "Sarah",
        "Mum's had another dizzy spell, in the bathroom first thing this time. That's three now.",
        "gt-11",
    ),
    (
        "2026-03-12 08:03",
        "Sarah",
        "She's alright, she sat on the edge of the bath until it passed.",
        "gt-11",
    ),
    ("2026-03-12 08:30", "Tom", "Is this worth ringing the surgery about?", ""),
    (
        "2026-03-12 08:44",
        "Sarah",
        "I've written them all down for the cardiology lot on the 17th.",
        "",
    ),
    ("2026-03-12 09:02", "Dot", "she never says a word to me about any of it", ""),
    (
        "2026-03-17 12:40",
        "Sarah",
        "Back from cardiology with Mum. They did an ECG, they're happy with how she is and there's no changes.",
        "gt-10",
    ),
    (
        "2026-03-17 12:42",
        "Sarah",
        "They want to see her again in the summer. I gave them the list of dizzy spells.",
        "gt-10",
    ),
    ("2026-03-17 12:55", "Tom", "That's a relief. Thanks for taking her.", ""),
    ("2026-03-17 13:10", "Dot", "thank goodness", ""),
    ("2026-03-17 13:20", "Priya", "Brilliant news 🙌", ""),
    (
        "2026-03-26 16:05",
        "Sarah",
        "The grab rails went in at Mum's today, one by the bath and one by the toilet.",
        "gt-12",
    ),
    ("2026-03-26 16:06", "Sarah", "<Media omitted>", ""),
    ("2026-03-26 16:20", "Priya", "She'll be glad of those.", ""),
    ("2026-03-26 16:31", "Tom", "What did they cost?", ""),
    ("2026-03-26 16:33", "Sarah", "Nothing, they came through the council.", "gt-12"),
    (
        "2026-03-29 18:40",
        "Sarah",
        "Mum's clocks are all an hour out. I've done the ones I can reach.",
        "",
    ),
    (
        "2026-04-02 19:22",
        "Sarah",
        "Mum's ankles are really puffy tonight, both of them. You can see the sock marks in them.",
        "gt-13",
    ),
    ("2026-04-02 19:24", "Sarah", "<Media omitted>", ""),
    ("2026-04-02 19:30", "Priya", "Has she had her feet up at all today?", "gt-13"),
    (
        "2026-04-02 19:44",
        "Sarah",
        "She's been in the chair since lunch. I've put a stool under them.",
        "",
    ),
    ("2026-04-02 20:10", "Tom", "Worth mentioning to the surgery if they're like it tomorrow.", ""),
    ("2026-04-03 08:15", "Sarah", "Ankles look normal this morning.", ""),
    (
        "2026-04-06 12:11",
        "Sarah",
        "Mum's got a blood test at Brookfield on Wednesday at 8.50, it's a fasting one so no breakfast.",
        "gt-14",
    ),
    ("2026-04-06 12:14", "Tom", "8.50 is early for her.", ""),
    ("2026-04-06 12:16", "Sarah", "I know. I'll be there for half eight to get her out.", ""),
    (
        "2026-04-08 09:40",
        "Sarah",
        "Bloods done. She was very good about the fasting, we went for a bacon barm straight after.",
        "gt-14",
    ),
    ("2026-04-08 09:52", "Priya", "😂 worth it", ""),
    ("2026-04-08 09:55", "Tom", "Nice one. When do they come back?", ""),
    ("2026-04-08 10:10", "Sarah", "A week or so they said.", ""),
    (
        "2026-04-09 19:12",
        "Dot",
        "my knee has been playing me up something rotten in this damp",
        "trap:dot-knee",
    ),
    ("2026-04-09 19:30", "Sarah", "Wrap up Auntie Dot", ""),
    (
        "2026-04-14 22:48",
        "Sarah",
        "Jean's just rung me. She went round at nine and the gas ring was on with nothing on it, and Mum couldn't say how long it had been like that.",
        "gt-15",
    ),
    (
        "2026-04-14 22:51",
        "Sarah",
        "She was in her nightie at nine in the evening and thought it was the morning.",
        "gt-15",
    ),
    ("2026-04-14 23:04", "Tom", "Right. That's frightening. Is she alright now?", ""),
    ("2026-04-14 23:09", "Sarah", "Jean sat with her till half nine. She's in bed.", ""),
    ("2026-04-14 23:15", "Priya", "Sarah do you want me to go over first thing?", ""),
    (
        "2026-04-15 07:12",
        "Sarah",
        "Morning. Rang Mum, she sounds like herself today and doesn't remember much about last night at all.",
        "gt-15",
    ),
    ("2026-04-15 07:20", "Tom", "We need to do something about that cooker.", ""),
    (
        "2026-04-15 07:31",
        "Dot",
        "our margaret has always been early to bed but that doesnt sound like her at all",
        "",
    ),
    (
        "2026-04-15 07:44",
        "Sarah",
        "I'm going to get a key safe put on the wall so Jean and the carers aren't stood there waiting for her to get to the door.",
        "gt-16",
    ),
    (
        "2026-04-15 07:52",
        "Tom",
        "Yes. I'll look at one of those cooker cut off gadgets as well.",
        "gt-16",
    ),
    ("2026-04-15 08:02", "Priya", "I'll pop in on my way home tonight anyway.", ""),
    (
        "2026-04-20 15:30",
        "Sarah",
        "Key safe is on, by the back door. The code's written in the front of the book in the kitchen drawer, Jean's got it and the carers have been told.",
        "gt-16",
    ),
    ("2026-04-20 15:41", "Tom", "Good. That's one less thing.", ""),
    (
        "2026-04-20 15:45",
        "~ Jean",
        "Thanks Sarah, all noted. Anything at all just shout, I'm only over the fence.",
        "",
    ),
    ("2026-04-20 15:50", "Sarah", "Thanks Jean 💐", ""),
    ("2026-04-20 16:02", "Dot", "thats a relief that is", ""),
    (
        "2026-04-21 11:05",
        "Sarah",
        "Podiatry have finally been in touch about Mum's feet. They said they'd get her in sometime next month and they'll write to her.",
        "gt-17",
    ),
    ("2026-04-21 11:09", "Priya", "About time, she's been waiting since Christmas.", ""),
    ("2026-04-21 11:15", "Dot", "shes been on at me about her feet for months", ""),
    (
        "2026-04-28 10:02",
        "Sarah",
        "Sorted Mum's repeat prescription. The apixaban's back on order and the pharmacy deliver on a Friday now.",
        "gt-18",
    ),
    ("2026-04-28 10:20", "Tom", "Do they charge for that?", ""),
    ("2026-04-28 10:25", "Sarah", "No, nothing.", ""),
    (
        "2026-05-02 15:10",
        "Sarah",
        "Mum's been out in the garden all afternoon deadheading, she's in her element.",
        "",
    ),
    ("2026-05-02 15:12", "Sarah", "<Media omitted>", ""),
    (
        "2026-05-02 15:40",
        "~ Jean",
        "She's been out there since after lunch, I took her a brew out 😊",
        "",
    ),
    (
        "2026-05-09 18:12",
        "Sarah",
        "Mum's had a fall in the bathroom this afternoon. She's alright but she's caught her arm on the towel rail and it's come up in a big bruise.",
        "gt-19",
    ),
    ("2026-05-09 18:14", "Sarah", "I've been round, nothing broken, she's having her tea.", ""),
    (
        "2026-05-09 18:51",
        "Tom",
        "Just had Mum on the phone. She's taken a tumble in the bathroom today and banged her arm, she says she's fine but I said I'd ring back later.",
        "gt-19",
    ),
    ("2026-05-09 18:53", "Sarah", "Tom see above 😂 I've been round already.", ""),
    ("2026-05-09 18:55", "Tom", "Ha. Didn't scroll up, sorry.", ""),
    ("2026-05-09 19:30", "Dot", "poor love", ""),
    ("2026-05-09 19:35", "Priya", "Does she want us to come over tomorrow?", ""),
    ("2026-05-09 19:50", "Sarah", "She says no. She's watching the snooker.", ""),
    (
        "2026-05-13 07:50",
        "Priya",
        "Alfie's got a temperature so he's off school today. Nothing dramatic, he's on the sofa with the iPad.",
        "trap:alfie-temperature",
    ),
    ("2026-05-13 08:02", "Sarah", "Poor lad. Ellie was the same last term.", ""),
    (
        "2026-05-20 14:40",
        "Sarah",
        "The referral to the memory service has gone in for Mum. Brookfield rang to say the wait is about three months.",
        "gt-20",
    ),
    ("2026-05-20 14:52", "Tom", "Three months. Right.", ""),
    ("2026-05-20 15:01", "Priya", "At least it's in the system now.", ""),
    ("2026-05-20 15:30", "Dot", "will they come out to the house", ""),
    ("2026-05-20 15:36", "Sarah", "I don't know yet, they'll write.", ""),
    (
        "2026-05-27 09:14",
        "Sarah",
        "X-ray for Mum's hip, Salford Royal, Tuesday 2 June at 3.40. Tom can you do this one, I'm in Birmingham that day.",
        "gt-21",
    ),
    ("2026-05-27 09:40", "Tom", "Yes I'll drive over. 3.40 Tuesday, it's in the diary.", "gt-21"),
    ("2026-05-27 09:44", "Sarah", "Thank you. She'll need picking up by 3.", ""),
    (
        "2026-05-27 09:50",
        "Tom",
        "Is she alright for the rest of the day or does someone need to be there?",
        "",
    ),
    ("2026-05-27 09:55", "Sarah", "Carers do the morning as usual.", ""),
    (
        "2026-06-02 16:20",
        "Tom",
        "Disaster. Got to Mum's at three and she's not in. Jean says she went to the lunch club and isn't back. We've missed the X-ray slot.",
        "gt-21",
    ),
    (
        "2026-06-02 16:35",
        "Sarah",
        "Oh Mum. Ring the department and see if they'll give us another one.",
        "",
    ),
    ("2026-06-02 17:02", "Tom", "Left a message. Nobody picks up after 4.", ""),
    ("2026-06-02 17:20", "Dot", "she never looks at that calendar", ""),
    ("2026-06-02 17:30", "Sarah", "Don't beat yourself up Tom, she does this.", ""),
    (
        "2026-06-09 13:05",
        "Sarah",
        "Got Mum an appointment with Dr Aziz for Wednesday next week, the 17th at 9.30. Face to face this time.",
        "gt-22",
    ),
    (
        "2026-06-09 13:07",
        "Sarah",
        "I want to go through the dizzy spells and the ankles with him properly.",
        "gt-22",
    ),
    (
        "2026-06-09 13:20",
        "Tom",
        "Good. Write it all down before you go, you always forget half of it.",
        "",
    ),
    ("2026-06-09 13:22", "Priya", "I can do Alfie's pick up on the Wednesday if that helps.", ""),
    (
        "2026-06-16 21:05",
        "Sarah",
        "Right, list for tomorrow so I don't forget:\n- the dizzy spells, three of them since January\n- the ankles swelling of an evening\n- whether her tablets need looking at",
        "gt-22",
    ),
    ("2026-06-16 21:20", "Tom", "👍", ""),
    ("2026-06-18 20:15", "Tom", "How did yesterday go in the end? I've heard nothing.", ""),
    (
        "2026-06-18 20:31",
        "Sarah",
        "Sorry, mad day. Dr Aziz was really good with her. He did her blood pressure lying down and then stood up, and it drops right off when she stands, which he says explains the dizziness.",
        "gt-22",
    ),
    (
        "2026-06-18 20:33",
        "Sarah",
        "At the appointment he halved her bisoprolol to 2.5mg and he wants to see her again in four weeks to see if it settles.",
        "gt-22,gt-23",
    ),
    ("2026-06-18 20:40", "Tom", "That's the first proper answer we've had out of anyone.", ""),
    ("2026-06-18 20:44", "Dot", "well thats something at last", ""),
    (
        "2026-06-19 08:02",
        "Sarah",
        "He's also asked us to write down every dizzy spell with the time of day. There's a pad on the kitchen calendar now.",
        "gt-22",
    ),
    ("2026-06-19 08:10", "Tom", "I'll ring her tonight and go through it with her.", ""),
    (
        "2026-06-25 18:10",
        "Tom",
        "Bramble's at the vets tomorrow for his booster. £60 for a jab is criminal.",
        "trap:dog-vet",
    ),
    ("2026-06-25 18:14", "Priya", "And he'll sulk for two days after.", ""),
    (
        "2026-06-28 19:30",
        "Tom",
        "Rang Mum for half an hour, mostly about next door's extension. She's on good form.",
        "",
    ),
    (
        "2026-07-02 11:40",
        "Sarah",
        "Another letter. Cardiology follow up for Mum on Thursday 6 August at 10.15, Salford Royal again.",
        "gt-24",
    ),
    ("2026-07-02 11:44", "Tom", "August. We're away the first week aren't we?", ""),
    ("2026-07-02 11:50", "Priya", "We're back on the 4th, it's fine.", ""),
    ("2026-07-02 12:05", "Sarah", "I'll take her. Putting it in the calendar now.", "gt-24"),
    ("2026-07-02 12:10", "Dot", "ill come with you if you like", ""),
    ("2026-07-02 12:15", "Sarah", "Yes do, she'd like that.", ""),
    (
        "2026-07-08 13:20",
        "Sarah",
        "Mum's got the fan going and all the curtains shut, she says it's like Benidorm in there.",
        "",
    ),
    ("2026-07-08 13:35", "Dot", "shes always hated the heat our margaret", ""),
    (
        "2026-07-11 10:15",
        "Sarah",
        "Mum's asking if Ellie wants the piano stool. It's been in the back bedroom since 1974.",
        "",
    ),
    ("2026-07-11 10:30", "Priya", "😂", ""),
]

# System lines, as WhatsApp writes them: a timestamp and no `Sender:` prefix. Stored but never
# extracted, and they make line offsets honest.
SYSTEM_LINES: list[tuple[str, str]] = [
    (
        "2026-01-12 08:02",
        "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.",
    ),
    ("2026-01-12 08:02", 'Sarah created group "Mum ❤️"'),
    ("2026-01-12 08:02", "Sarah added you"),
    ("2026-01-13 10:15", "Dot joined using this group's invite link"),
    ("2026-03-01 12:04", "Sarah changed this group's icon"),
    ("2026-04-20 15:20", "Sarah added ~ Jean"),
    ("2026-05-30 21:14", "Priya: This message was deleted"),
    ("2026-06-21 17:33", "You deleted this message"),
    ("2026-07-01 08:00", "Sarah changed the group description"),
]

# --------------------------------------------------------------------------------------
# Filler. Ordinary family chatter, generated from these templates with a fixed seed.
# Nothing here may be readable as a care event — `_assert_filler_is_inert` enforces it.
# --------------------------------------------------------------------------------------

SLOTS: dict[str, list[str]] = {
    "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "meal": [
        "chilli",
        "fish and chips",
        "a curry",
        "jacket potatoes",
        "beans on toast",
        "spag bol",
        "a roast",
        "pizza",
    ],
    "team": ["Leeds", "United", "City", "Salford", "the girls' team"],
    "weather": [
        "chucking it down",
        "roasting",
        "freezing",
        "blowing a gale",
        "grim out",
        "actually quite nice",
    ],
    "time": ["half seven", "eight", "half eight", "nine", "half nine", "quarter past six"],
    "shop": ["Aldi", "Tesco", "the Co-op", "Home Bargains", "B&M", "the big Asda"],
    "show": [
        "the new series",
        "Bake Off",
        "the match",
        "Gogglebox",
        "that documentary",
        "the quiz",
    ],
}

TEMPLATES: dict[str, list[str]] = {
    "Sarah": [
        "Ellie has missed the bus again, I'm doing a taxi run",
        "Traffic on the A6 is unbelievable this morning",
        "Has anyone else's bins not been done",
        "Put a wash out and it's started raining, obviously",
        "Working from home today if anyone needs me",
        "Ellie's parents evening is on the {day}, joy",
        "Just spent £40 in {shop} and I've still got nothing for tea",
        "The dishwasher is making that noise again",
        "Ellie wants driving lessons apparently. She's sixteen.",
        "Right I'm off out for a walk before it goes dark",
        "Doing {meal} tonight if anyone's passing",
        "It is absolutely {weather} out there",
        "Who's watching {show} tonight",
        "Ellie has decided she's vegetarian. Until Sunday.",
        "Anyone want two tickets for the thing at the Lowry, we can't go now",
        "Car's in for its service, I'm on the tram all week",
        "I have got 400 unread emails and I've been off two days",
        "Ellie's revision timetable has lasted precisely one afternoon",
        "Off at {time} tomorrow if anyone wants a lift into town",
        "The fence panel has gone over again",
        "Trying to book a table for the {day}, everywhere's full",
        "Just seen the electric bill 😩",
        "Ellie's mate is staying over, expect noise",
        "Anyone got a phone charger they're not using, mine's chewed",
        "I'm going to bed, I'm shattered",
        "Bin day moved because of the bank holiday, don't get caught out",
        "New coffee machine has arrived and I don't understand it",
        "Ellie got an A in her mock. I'm saying nothing about the timetable.",
        "Postman's left a parcel in the recycling bin again",
        "Anyone free {day} afternoon",
        "That's the shopping delivered, three things missing as usual",
        "The cat has brought a bird in. Again.",
        "Long day. {meal} and telly.",
        "Ellie's asking about a job at the coffee place in town",
        "Does anyone want these garden chairs, they're just sat in the shed",
        "Painting the back bedroom this weekend if anyone's bored",
        "It's {weather}, don't hang anything out",
        "Just got in, sorry, missed all this",
        "Ellie is playing that same song for the fortieth time",
        "Boiler man's coming {day} between eight and one, obviously",
    ],
    "Tom": [
        "Train's cancelled again. Standing room only on the next one.",
        "{team} were dreadful. That's an hour of my life gone.",
        "Alfie's got football training tonight, it's {weather}",
        "In back to back meetings till three if I'm quiet",
        "Anyone recorded {show}",
        "Alfie has lost another water bottle",
        "Leeds ring road at half four is a car park",
        "Just had a parking ticket in the post from March 🙄",
        "Working late tonight, don't ring the house phone",
        "Alfie's team won 4-1, he scored one and claimed two",
        "Who's got the wifi password for the caravan",
        "Bramble has eaten an entire loaf off the side",
        "Do we know what we're doing about the {day}",
        "Off to the tip with a car full of cardboard",
        "Alfie's swimming badge came, he's very pleased with himself",
        "Anyone want a shed. Free. You collect.",
        "That's me done for the week, opening something cold",
        "M62 is shut both ways, I'm going the long way round",
        "Alfie's school photo has arrived and he's pulling a face in every one",
        "Booked the caravan for August, same place as last year",
        "Priya's on lates all week so I'm doing the school run",
        "£90 to fill the car up. Ninety.",
        "Anyone else's broadband gone off",
        "Alfie wants a bike for his birthday, a proper one",
        "Going for a run before it gets dark, back in an hour",
        "Just found a fiver in a coat pocket, it's my day",
        "Fantasy league is a disaster this week",
        "Right, {meal} and an early night",
        "Alfie has homework about the Romans and I've googled more than he has",
        "The garage rang, it's the exhaust. Of course it is.",
        "{team} away on the {day}, I've got a ticket if anyone wants the other",
        "Can someone remind me to put the bin out",
        "Alfie's football has gone over next door's fence for the third time",
        "Priya's made enough {meal} for nine people",
        "Nothing on telly. Absolutely nothing.",
        "Back on Sunday night, driving down after lunch",
        "Alfie lost a tooth and is negotiating hard",
    ],
    "Priya": [
        "On lates again this week, so I'll be asleep when you're all up",
        "Made too much {meal}, shout if you want some",
        "Alfie has been an angel today, which is suspicious",
        "Anyone know a decent plumber round here",
        "The tomatoes have finally done something 🍅",
        "School have sent four emails today. Four.",
        "It's {weather} so I'm not moving from this sofa",
        "Just did a big {shop} shop, never again on a Saturday",
        "Alfie's reading book is about a badger and he is not impressed",
        "Does anyone want these jars, I've got about thirty",
        "Watching {show} finally, don't spoil it",
        "Back at {time}, save me some",
        "My car's making a noise, Tom says it's fine which means it isn't",
        "Alfie's got a class assembly on the {day} if anyone's free",
        "Put the heating on, I've caved",
        "That's the ironing done for another decade",
        "Anyone got a good recipe for a tray bake",
        "Alfie has asked me nine questions about volcanoes",
        "Managed a whole coffee while it was still hot, big win",
        "New neighbours have moved in over the road, very smart van",
        "The garden is a jungle, I've given up",
        "Two days off in a row, I don't know myself",
        "Alfie wants to know if he can have a hamster. He cannot.",
        "Sorry, only just seen this, phone was on silent",
        "Everything in the freezer is unlabelled and I regret it",
        "Just planted out the beans, fingers crossed",
        "Alfie's football kit is somewhere in this house and I cannot find it",
        "Right, {meal} then bed",
        "Is anyone else getting these scam texts about parcels",
        "Sun's out, washing's out ☀️",
        "Book club moved to the {day} because half of them are away",
    ],
    "Dot": [
        "its blowing a gale here",
        "who won the quiz last night",
        "im on the bus, be there for {time}",
        "the shop had no bread again",
        "watching {show}, its rubbish",
        "the bingo was busy tonight",
        "my Freeview box has gone funny",
        "lovely day for it",
        "ive made a cake if anyone wants a slice",
        "the buses are all over the place today",
        "i cant work this phone out at all",
        "its {weather} isnt it",
        "was it you who rang me at half eight",
        "our Sarah is doing too much as usual",
        "next doors cat has been on my shed roof again",
        "the market was heaving",
        "im having a quiet one tonight",
        "did anyone see the state of the road works",
        "off out with Brenda for a coffee",
        "the price of butter is a scandal",
        "hello all",
        "goodnight all x",
        "i knew that would happen",
        "very good 😂",
        "the raffle was rigged if you ask me",
    ],
    "~ Jean": [
        "Morning all ☀️",
        "It's turned lovely out here",
        "Bins are out, I've done both",
        "Post's been, nothing exciting",
        "Lovely evening for sitting out",
        "That wind's had my hanging basket over",
    ],
}

SHORT_REPLIES = [
    "ok",
    "👍",
    "😂",
    "thanks",
    "yes",
    "no worries",
    "ha",
    "same",
    "will do",
    "sounds good",
    "🙌",
    "❤️",
    "on my way",
    "5 mins",
    "yep",
    "brilliant",
    "oh no",
    "😩",
    "nice one",
    "agreed",
    "sorry only just seen this",
    "🤣",
    "true",
    "can't tonight",
    "😊",
]

MEDIA = "<Media omitted>"

# Hand-written filler: the long birthday-present thread. Chatty, multi-day, multi-line, and
# completely inert — it exists to punish an extractor that treats any planning thread as care.
BIRTHDAY_THREAD: list[tuple[str, str, str]] = [
    (
        "2026-03-05 19:02",
        "Sarah",
        "Right. Ellie's sixteenth. Three weeks on Saturday and I have got nothing.",
    ),
    ("2026-03-05 19:04", "Tom", "What does she actually want"),
    (
        "2026-03-05 19:05",
        "Sarah",
        "She says she doesn't want anything, which means she wants something specific and expensive",
    ),
    ("2026-03-05 19:07", "Priya", "Is she still after a record player"),
    (
        "2026-03-05 19:08",
        "Sarah",
        "She was in January. Now it's a camera. Next week it'll be something else.",
    ),
    ("2026-03-05 19:11", "Dot", "money in a card is what they all want"),
    ("2026-03-05 19:12", "Tom", "Dot's right you know"),
    ("2026-03-05 19:14", "Sarah", "She'll spend it in a week and have nothing to show for it"),
    ("2026-03-05 19:15", "Priya", "That's sort of the point of sixteen though isn't it"),
    ("2026-03-05 19:20", "Sarah", MEDIA),
    ("2026-03-05 19:21", "Sarah", "That's the record player she showed me. £160."),
    ("2026-03-05 19:24", "Tom", "That's not bad actually. We could go in on it."),
    (
        "2026-03-05 19:26",
        "Priya",
        "Ideas so far:\n- record player\n- camera\n- money towards driving lessons for next year\n- concert tickets\nShout if you've got better ones",
    ),
    ("2026-03-05 19:30", "Dot", "she can have a bit off me for it as well"),
    ("2026-03-05 19:31", "Sarah", "Dot you don't have to"),
    ("2026-03-05 19:33", "Dot", "im doing it, dont argue"),
    (
        "2026-03-05 19:40",
        "Tom",
        "Right so record player as the main one, and everyone puts in what they want",
    ),
    ("2026-03-05 19:41", "Priya", "👍"),
    ("2026-03-05 20:02", "Sarah", "What about the records though, she's got none"),
    ("2026-03-05 20:04", "Tom", "I've got a box in the loft she'd probably like"),
    ("2026-03-05 20:05", "Sarah", "She would actually love that"),
    ("2026-03-05 20:06", "Tom", "Don't tell her, I want to see her face"),
    (
        "2026-03-06 08:12",
        "Priya",
        "Thinking about it, is she going to think a record player is a bit much",
    ),
    ("2026-03-06 08:20", "Sarah", "No. She'll be thrilled and then use her phone anyway."),
    ("2026-03-06 08:22", "Tom", "😂"),
    (
        "2026-03-06 08:40",
        "Sarah",
        "I'll order it today then. Delivery's five days so it'll be here well before.",
    ),
    ("2026-03-06 12:15", "Sarah", "Ordered ✅"),
    ("2026-03-06 12:16", "Priya", "🙌"),
    ("2026-03-06 12:30", "Dot", "what about a cake"),
    (
        "2026-03-06 12:41",
        "Sarah",
        "She wants that place in town to do it. Chocolate, apparently, with the drip icing.",
    ),
    ("2026-03-06 12:44", "Priya", "I'll order that, I'm in town Thursday"),
    (
        "2026-03-07 10:05",
        "Tom",
        "Found the records. Some of these are rough but there's about twenty decent ones.",
    ),
    ("2026-03-07 10:06", "Tom", MEDIA),
    ("2026-03-07 10:22", "Sarah", "Oh she'll be made up with those"),
    ("2026-03-07 10:30", "Dot", "i had that one on the corner"),
    ("2026-03-07 10:44", "Priya", "Cake ordered, collecting on the Saturday morning"),
    ("2026-03-07 10:45", "Sarah", "You are all brilliant, thank you"),
    ("2026-03-21 09:02", "Sarah", "She's SIXTEEN. How."),
    ("2026-03-21 09:10", "Tom", "Happy birthday Ellie 🎉"),
    ("2026-03-21 09:12", "Priya", "Happy birthday!! 🎂"),
    ("2026-03-21 09:20", "Dot", "happy birthday love xx"),
    ("2026-03-21 14:30", "Sarah", MEDIA),
    (
        "2026-03-21 14:31",
        "Sarah",
        "Face was worth it. Records went down even better than the player.",
    ),
    ("2026-03-21 14:40", "Tom", "Told you 😁"),
]

# Words that mean a line could be read as care content. Filler containing any of these is a
# bug in the fixture, not a nuisance: the eval counts an extraction from filler as a false
# positive, so an accidentally care-shaped filler line would punish a correct extractor.
CARE_WORDS = [
    "mum",
    "mam",
    "nan",
    "nana",
    "gran",
    "granny",
    "margaret",
    "doctor",
    "doctors",
    "dr",
    "gp",
    "nurse",
    "surgery",
    "hospital",
    "clinic",
    "ward",
    "ambulance",
    "paramedic",
    "999",
    "111",
    "consultant",
    "outpatients",
    "appointment",
    "appointments",
    "appt",
    "referral",
    "referred",
    "x-ray",
    "xray",
    "scan",
    "blood",
    "bloods",
    "ecg",
    "results",
    "tablet",
    "tablets",
    "pill",
    "pills",
    "dose",
    "doses",
    "medication",
    "medicine",
    "prescription",
    "pharmacy",
    "apixaban",
    "bisoprolol",
    "co-codamol",
    "paracetamol",
    "fall",
    "fell",
    "fallen",
    "trip",
    "dizzy",
    "dizziness",
    "faint",
    "poorly",
    "unwell",
    "ill",
    "sick",
    "pain",
    "ache",
    "aching",
    "hurt",
    "bruise",
    "bruised",
    "swollen",
    "ankles",
    "hip",
    "knee",
    "chest",
    "breath",
    "breathless",
    "temperature",
    "infection",
    "carer",
    "carers",
    "care",
    "physio",
    "podiatry",
    "memory",
    "confused",
    "confusion",
    "dementia",
    "stroke",
    "heart",
    "af",
    "wobbly",
    "unsteady",
    "collapse",
    "hoist",
]
_CARE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in CARE_WORDS) + r")\b", re.I)


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Record:
    """One rendered message. `rank` orders same-minute collisions so sorting is total."""

    dt: datetime  # naive Europe/London wall clock, exactly as the export shows it
    rank: int  # 0 system, 1 scripted, 2 hand-written filler, 3 generated filler
    seq: int
    sender: str | None  # None = system line, rendered without a "Sender: " prefix
    text: str
    tag: str


def _local(spec: str) -> datetime:
    return datetime.strptime(spec, "%Y-%m-%d %H:%M")


def _stamp(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year}, {dt.hour:02d}:{dt.minute:02d} - "


def _fill(rng: random.Random, template: str) -> str:
    while "{" in template:
        key = template[template.index("{") + 1 : template.index("}")]
        template = template.replace("{" + key + "}", rng.choice(SLOTS[key]), 1)
    return template


# Hours weighted the way a family chat actually behaves: a morning burst, a lunchtime dip,
# a long evening tail. Flat hours make the quiet-gap chunker behave unrealistically.
_HOUR_WEIGHTS = [
    (7, 6),
    (8, 9),
    (9, 8),
    (10, 6),
    (11, 5),
    (12, 6),
    (13, 5),
    (14, 4),
    (15, 5),
    (16, 7),
    (17, 8),
    (18, 10),
    (19, 12),
    (20, 12),
    (21, 9),
    (22, 4),
]
_HOURS = [h for h, w in _HOUR_WEIGHTS for _ in range(w)]
_ALL_SENDERS = ["Sarah", "Tom", "Priya", "Dot", "~ Jean"]
_WEIGHT = {"Sarah": 34, "Tom": 25, "Priya": 24, "Dot": 17, "~ Jean": 5}
# Must match the "joined"/"added" system lines above, or the transcript contradicts itself.
JOINED = {"Dot": _local("2026-01-13 10:15"), "~ Jean": _local("2026-04-20 15:20")}


def _filler_records(rng: random.Random, start_seq: int) -> list[Record]:
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    counts = [rng.choice([6, 7, 8, 9, 9, 10, 11, 12, 13, 15]) for _ in days]
    # Nudge to exactly TARGET_FILLER so the file size is a fixed number, not a seed artefact.
    while sum(counts) != TARGET_FILLER:
        i = rng.randrange(len(counts))
        if sum(counts) > TARGET_FILLER and counts[i] > 4:
            counts[i] -= 1
        elif sum(counts) < TARGET_FILLER and counts[i] < 18:
            counts[i] += 1

    out: list[Record] = []
    seq = start_seq
    for day, n in zip(days, counts, strict=True):
        times = sorted((rng.choice(_HOURS), rng.randrange(60)) for _ in range(n))
        if day == START:
            # Nothing may predate the group-creation header: a real export cannot contain a
            # message sent before the group existed, and a parser test built on one is a lie.
            times = [(max(hour, 9), minute) for hour, minute in times]
        for hour, minute in times:
            at = datetime(day.year, day.month, day.day, hour, minute)
            # Dot and Jean are added to the group partway through; they cannot chat before it.
            senders = [s for s in _ALL_SENDERS if JOINED.get(s, at) <= at]
            weights = [_WEIGHT[s] for s in senders]
            sender = rng.choices(senders, weights=weights, k=1)[0]
            roll = rng.random()
            if roll < 0.045:
                text = MEDIA
            elif roll < 0.34:
                text = rng.choice(SHORT_REPLIES)
            elif roll < 0.355:
                text = (
                    _fill(rng, rng.choice(TEMPLATES[sender]))
                    + "\n"
                    + _fill(rng, rng.choice(TEMPLATES[sender]))
                )
            else:
                text = _fill(rng, rng.choice(TEMPLATES[sender]))
            out.append(Record(at, 3, seq, sender, text, ""))
            seq += 1
    return out


def _records(seed: int) -> list[Record]:
    rng = random.Random(seed)
    recs: list[Record] = []
    seq = 0
    for when, text in SYSTEM_LINES:
        recs.append(Record(_local(when), 0, seq, None, text, ""))
        seq += 1
    for when, sender, text, tag in SCRIPT:
        recs.append(Record(_local(when), 1, seq, sender, text, tag))
        seq += 1
    for when, sender, text in BIRTHDAY_THREAD:
        recs.append(Record(_local(when), 2, seq, sender, text, ""))
        seq += 1
    recs.extend(_filler_records(rng, seq))
    recs.sort(key=lambda r: (r.dt, r.rank, r.seq))
    return recs


def _render(recs: list[Record]) -> tuple[list[str], dict[int, int]]:
    """Render to export lines and map each record's `seq` to its 1-based first line number."""
    lines: list[str] = []
    first_line: dict[int, int] = {}
    for rec in recs:
        body = rec.text.split("\n")
        head = _stamp(rec.dt) + (f"{rec.sender}: " if rec.sender else "") + body[0]
        lines.append(head)
        first_line[rec.seq] = len(lines)
        lines.extend(body[1:])
    return lines, first_line


def _iso_utc(spec: str, precision: str) -> str:
    if precision == "exact":
        aware = _local(spec).replace(tzinfo=TZ).astimezone(UTC)
        return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
    if precision == "month":
        return f"{spec}-01T00:00:00Z"
    return f"{spec}T00:00:00Z"


def _ground_truth(recs: list[Record], first_line: dict[int, int]) -> list[dict]:
    by_event: dict[str, list[int]] = {}
    for rec in recs:
        if not rec.tag or rec.tag.startswith("trap:"):
            continue
        for event_id in rec.tag.split(","):
            by_event.setdefault(event_id, []).append(first_line[rec.seq])
    return [
        {
            "id": e["id"],
            "kind": e["kind"],
            "occurred_at": _iso_utc(e["occurred_at"], e["occurred_at_precision"]),
            "occurred_at_precision": e["occurred_at_precision"],
            "title": e["title"],
            "natural_key_hint": e["natural_key_hint"],
            "must_mention": e["must_mention"],
            "source_line_numbers": sorted(by_event[e["id"]]),
            "notes": e["notes"],
        }
        for e in EVENTS
    ]


# --------------------------------------------------------------------------------------
# Verification. Runs on every generation, because a fixture that has quietly drifted from
# its labels is worse than no fixture: the eval would score a correct extractor as broken.
# --------------------------------------------------------------------------------------

# The two overnight gaps a chunk break must land in: the confusion/key-safe thread (gt-15,
# gt-16) and the GP outcome thread (gt-22, gt-23).
BOUNDARY_THREAD_GAPS = [
    (_local("2026-04-14 23:15"), _local("2026-04-15 07:12")),
    (_local("2026-06-18 20:44"), _local("2026-06-19 08:02")),
]


def _chunk_break_gaps(recs: list[Record]) -> list[tuple[datetime, datetime]]:
    """Where Track A's chunker would close a chunk (appendix §3 constants). Extraction skips
    system lines, so neither does this."""
    msgs = [r for r in recs if r.sender is not None]
    gaps: list[tuple[datetime, datetime]] = []
    start = 0
    for i in range(1, len(msgs)):
        n = i - start
        gap = msgs[i].dt - msgs[i - 1].dt
        span = msgs[i].dt - msgs[start].dt
        if (
            (n >= CHUNK_MIN_FOR_GAP_BREAK and gap >= CHUNK_QUIET_GAP)
            or n >= CHUNK_HARD_MAX
            or span > CHUNK_MAX_SPAN
        ):
            gaps.append((msgs[i - 1].dt, msgs[i].dt))
            start = i
    return gaps


def _verify(
    recs: list[Record], lines: list[str], first_line: dict[int, int], events: list[dict]
) -> None:
    ids = [e["id"] for e in EVENTS]
    assert len(ids) == 24, f"the eval is written against 24 events, got {len(ids)}"
    assert len(set(ids)) == 24, "duplicate event id"
    for e in EVENTS:
        assert e["kind"] in {"symptom", "appointment", "medication", "note"}, e["id"]
        assert e["occurred_at_precision"] in {"exact", "day", "week", "month", "unknown"}, e["id"]
        if e["occurred_at_precision"] == "week":
            assert date.fromisoformat(e["occurred_at"]).weekday() == 0, (
                f"{e['id']} week must start Monday"
            )
        if e["occurred_at_precision"] == "month":
            assert len(e["occurred_at"]) == 7, f"{e['id']} month must be YYYY-MM"

    tagged = {
        t for _, _, _, tag in SCRIPT for t in tag.split(",") if t and not t.startswith("trap:")
    }
    assert tagged == set(ids), f"SCRIPT/EVENTS mismatch: {tagged ^ set(ids)}"

    for e in events:
        assert e["source_line_numbers"], f"{e['id']} has no source lines"
        # A must_mention the transcript never says is a trap for a correct extractor, not a
        # test of one — so every one of them has to be present in the lines we point at.
        cited = " ".join(lines[n - 1] for n in e["source_line_numbers"]).casefold()
        for phrase in e["must_mention"]:
            assert phrase.casefold() in cited, (
                f"{e['id']} must_mention {phrase!r} is not in its sources"
            )

    # Every source_line_number must point at the exact line the label was derived from.
    for rec in recs:
        if not rec.tag:
            continue
        expected = _stamp(rec.dt) + f"{rec.sender}: " + rec.text.split("\n")[0]
        got = lines[first_line[rec.seq] - 1]
        assert got == expected, f"line {first_line[rec.seq]} is {got!r}, expected {expected!r}"

    # Filler must be genuinely non-extractable, not merely intended to be.
    for rec in recs:
        if rec.rank < 2:
            continue
        hit = _CARE_RE.search(rec.text)
        assert hit is None, (
            f"filler at {rec.dt} reads as care content: {hit.group(0)!r} in {rec.text!r}"
        )

    gaps = _chunk_break_gaps(recs)
    for want in BOUNDARY_THREAD_GAPS:
        assert want in gaps, (
            f"the {want[0]:%d}-{want[1]:%d %b} thread no longer straddles a chunk boundary; "
            f"nearest breaks: {[g for g in gaps if abs(g[0] - want[0]) < timedelta(days=6)]}"
        )

    messages = [r for r in recs if r.sender is not None]
    assert 1900 <= len(messages) <= 2100, f"{len(messages)} messages, wanted ~2000"
    span = (messages[-1].dt - messages[0].dt).days
    assert span >= 175, f"only {span} days of history"


def build(seed: int = SEED) -> tuple[list[str], list[dict], list[Record]]:
    recs = _records(seed)
    lines, first_line = _render(recs)
    events = _ground_truth(recs, first_line)
    _verify(recs, lines, first_line, events)
    return lines, events, recs


def main() -> None:
    lines, events, recs = build()
    FIXTURES.mkdir(exist_ok=True)
    TXT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    JSON_PATH.write_text(
        json.dumps(events, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )

    messages = [r for r in recs if r.sender is not None]
    senders: dict[str, int] = {}
    for r in messages:
        senders[r.sender] = senders.get(r.sender, 0) + 1
    print(
        f"{TXT_PATH}: {len(lines)} lines, {len(messages)} messages, "
        f"{len(lines) - len(recs)} continuations, {len(SYSTEM_LINES)} system lines"
    )
    print(
        f"span: {messages[0].dt:%d/%m/%Y} to {messages[-1].dt:%d/%m/%Y} "
        f"({(messages[-1].dt - messages[0].dt).days} days)"
    )
    print("senders: " + ", ".join(f"{k}={v}" for k, v in sorted(senders.items())))
    print(f"{JSON_PATH}: {len(events)} labelled events")
    print(f"chunk breaks (simulated): {len(_chunk_break_gaps(recs))}")


if __name__ == "__main__":
    main()
