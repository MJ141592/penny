# The demo family fixture

`demo_family.txt` is an invented WhatsApp group export. `demo_family_events.json` is its
labelled ground truth: **exactly 24 care events**, hand-placed, each one pointing at the lines
it was written from.

Together they are the **M1 go/no-go for the product concept**. If extraction can't find these
events in this transcript, Penny does not work, and it is cheaper to learn that here than
after the schema, the API and the UI are built on top of it.

Nothing in here is real. The family, the surgery, the hospital, the conditions and the
medications are all invented for the eval.

## What it is

| | |
|---|---|
| Format | Android WhatsApp export, `dd/mm/yyyy, HH:MM - Sender: text` |
| Dayfirst | **true** — the parser takes this explicitly, it never sniffs |
| Timezone | **Europe/London** — exports carry no offset at all, so this is an input, not a fact in the file |
| Span | 12/01/2026 – 12/07/2026 (181 days) |
| Volume | 2,062 lines = 2,014 messages + 39 continuation lines + 9 system lines |
| Senders | Sarah 669, Tom 508, Priya 460, Dot 343, `~ Jean` 34 |
| Care content | 170 hand-written messages carrying the 24 events; the other ~1,844 are inert filler (44 of them a hand-written birthday-present thread) |

The cast: **Margaret Doyle**, 84, "Mum"/"Nan"/"our Margaret", lives alone in Salford with
weekday-morning carer visits. **Sarah** (daughter, coordinator), **Tom** (son, in Leeds),
**Priya** (Tom's wife), **Dot** (Margaret's sister), **~ Jean** (next door, added in April —
the `~` prefix is how WhatsApp renders a non-contact, and the parser has to survive it).
Conditions the family mentions: atrial fibrillation, osteoarthritis of the left hip, and
memory problems in their own words, undiagnosed. Drugs: apixaban, bisoprolol, co-codamol.
GP is Dr Aziz at Brookfield surgery; cardiology is at Salford Royal.

## The 24 events

| id | kind | occurred_at | precision | title | must_mention |
|---|---|---|---|---|---|
| `gt-01` | symptom | 2026-01-19 | day | Dizzy spell standing up in the kitchen | `dizz` |
| `gt-02` | medication | 2026-01-22 | day | Missed two doses of apixaban | `apixaban` |
| `gt-03` | appointment | 2026-01-29 11:20Z | exact | Telephone appointment with Dr Aziz | `aziz` |
| `gt-04` | note | 2026-02-02 | day | Carer morning visit moved to 9.30 | `visit` |
| `gt-05` | appointment | 2026-02-24 14:15Z | exact | Cardiology outpatients, Salford Royal (cancelled) | `cardiolog`, `cancel` |
| `gt-06` | symptom | 2026-02-10 | day | Fall in the hall, bruised hip | `fall` |
| `gt-07` | symptom | 2026-02-16 | day | Dizzy spell at the kitchen sink | `dizz` |
| `gt-08` | medication | 2026-02-21 | day | Started co-codamol for hip pain | `co-codamol` |
| `gt-09` | appointment | week of 2026-03-16 | week | Physio home visit for the hip | `physio` |
| `gt-10` | appointment | 2026-03-17 11:00Z | exact | Cardiology outpatients, Salford Royal | `cardiolog` |
| `gt-11` | symptom | 2026-03-12 | day | Dizzy spell in the bathroom | `dizz` |
| `gt-12` | note | 2026-03-26 | day | Grab rails fitted in the bathroom | `grab rail` |
| `gt-13` | symptom | 2026-04-02 | day | Swollen ankles in the evening | `ankle` |
| `gt-14` | appointment | 2026-04-08 07:50Z | exact | Fasting blood test at Brookfield surgery | `blood` |
| `gt-15` | symptom | 2026-04-14 | day | Confusion in the evening, gas ring left on | `gas` |
| `gt-16` | note | 2026-04-20 | day | Key safe fitted by the back door | `key safe` |
| `gt-17` | appointment | 2026-05 | month | Podiatry appointment to be sent for May | `podiatry` |
| `gt-18` | medication | 2026-04-28 | day | Apixaban repeat prescription reordered | `apixaban` |
| `gt-19` | symptom | 2026-05-09 | day | Fall in the bathroom, bruised arm | `fall` |
| `gt-20` | note | 2026-05-20 | day | Referral to the memory service submitted | `memory` |
| `gt-21` | appointment | 2026-06-02 14:40Z | exact | Hip X-ray at Salford Royal (missed) | `x-ray` |
| `gt-22` | appointment | 2026-06-17 08:30Z | exact | GP appointment with Dr Aziz, Brookfield surgery | `aziz` |
| `gt-23` | medication | 2026-06-17 | day | Bisoprolol halved to 2.5mg | `bisoprolol` |
| `gt-24` | appointment | 2026-08-06 09:15Z | exact | Cardiology follow-up, Salford Royal | `cardiolog` |

Nine appointments, seven symptoms, four medications, four notes. `gt-24` is deliberately dated
after the export ends — it is the one event that should land in `/api/upcoming`.

## The hazards, and which event carries each

The mess is the point. Every one of these is a documented failure mode from the plan, planted
on purpose:

| Hazard | Events | What must happen |
|---|---|---|
| Cancelled then rebooked | `gt-05` → `gt-10` | **Two** events. The February one survives as `status: cancelled`; the March one is a separate row, not a duplicate and not an edit of it. Sarah even says "the one they cancelled in February has moved to the 17th", which is the bait. |
| Symptom recurring three times | `gt-01`, `gt-07`, `gt-11` | **Three** events weeks apart, same symptom. "That's three now" in the third message tempts a single summarising event. |
| Two siblings, one event | `gt-19` | Sarah at 18:12 ("a fall"), Tom at 18:51 ("a tumble"), 39 minutes apart, neither having read the other. **One** event. |
| Discussed before, reported after | `gt-22` | Booked 9 June, a multi-line list of questions on the 16th, and the outcome reported on the evening of the **18th** — a day after it happened, in a separate thread. **One** appointment that gains `details.outcome`, `follow_up_actions` and `status: attended`. There is no `appointment_outcome` kind and no `parent_event_id`. Same shape, easier, at `gt-10` and `gt-14`. |
| Relative-only dates | `gt-06` (day), `gt-09` (week), `gt-17` (month) | "last Tuesday" → 10 Feb; "the week after next" → week of 16 Mar; "sometime next month" → May. Each resolves against **the timestamp of the message it appears in**, and a precision finer than the label is a wrong answer, not a better one. |
| Thread across a chunk boundary | `gt-15` + `gt-16`, and `gt-22` + `gt-23` | Both threads run over an overnight quiet gap where the chunker closes a chunk (14→15 April, 18→19 June). The generator simulates the chunker and **fails** if either boundary moves, so this stays true as the fixture changes. |
| Looks like care, isn't | five distractors | See below. |
| Media and system lines | throughout | 90-odd `<Media omitted>` placeholders, `Sarah created group "Mum ❤️"`, invite-link joins, `This message was deleted`, `Sarah added ~ Jean`. |

### The distractors

Not in the ground truth, and extracting any of them is a **false positive**. All five are about
a person who is not the care recipient — the extractor has to notice that.

| When | Who | Line |
|---|---|---|
| 25/02 08:30 | Priya | "I've got the dentist at 11 so I'll be off my phone for an hour." |
| 09/03 21:40 | Sarah | "My head has been banging all day. I've taken two paracetamol and I'm going to bed." |
| 09/04 19:12 | Dot | "my knee has been playing me up something rotten in this damp" |
| 13/05 07:50 | Priya | "Alfie's got a temperature so he's off school today." |
| 25/06 18:10 | Tom | "Bramble's at the vets tomorrow for his booster." |

## Acceptance thresholds

From the plan, written before the first run, and not to be renegotiated after seeing a score:

- **≥ 20 of 24** events found, with the correct date **to day precision**
- **≤ 2** invented events — anything not traceable to a real message
- **≤ 2** duplicate pairs surviving the merge

Run:

```
uv run python -m scripts.extract_export fixtures/demo_family.txt --dayfirst --tz Europe/London
```

## Reading `demo_family_events.json`

A JSON array of 24 objects, in transcript order. Per object:

| Field | Meaning |
|---|---|
| `id` | `gt-01` … `gt-24`. Stable; cite these in eval output. |
| `kind` | One of the four event kinds. |
| `occurred_at` | ISO 8601 **UTC with a trailing `Z`**, matching `docs/api-contract.md`. |
| `occurred_at_precision` | `exact` \| `day` \| `week` \| `month` \| `unknown`. |
| `title` | Human label. Illustrative — **do not** string-match extraction against it. |
| `natural_key_hint` | Roughly what the model's `natural_key` should normalise to, so two mentions of one event land on the same `dedup_key`. A hint for judging merges, not an exact expected value. |
| `must_mention` | Substrings a correct extraction must contain, matched **case-insensitively** anywhere in `title` + `body` + `details`. Deliberately stemmed (`dizz`, `cardiolog`) so "dizziness"/"dizzy" and "cardiology"/"cardiologist" both pass. Every one is verified to appear in the cited source lines. |
| `source_line_numbers` | **1-based** line numbers in `demo_family.txt`, pointing at the *first* line of each message that carries the event (a multi-line message occupies several lines; the number is its header line). |
| `notes` | Why this one is hard. |

`occurred_at` encodes precision rather than pretending to a clock it doesn't have:

- `exact` — the local wall-clock time converted to UTC (so BST events shift by an hour)
- `day` — that date at `00:00:00Z`
- `week` — the **Monday** of that ISO week at `00:00:00Z`
- `month` — the 1st at `00:00:00Z`

Week and month labels are never timezone-converted: converting local midnight would push the
bucket into the previous week or month during BST and silently corrupt the label. Compare
non-`exact` events by date, not by instant.

## Regenerating

```
cd backend && uv run python -m scripts.make_demo_fixture
```

Both files are generated, and **both are committed** — the eval must not depend on being able
to re-run a generator. The script is byte-reproducible: the ~1,840 filler messages come from
`random.Random(SEED)` with a fixed seed, and the output is written with an explicit `\n`
newline and utf-8 encoding. Run it twice, `git status` stays clean.

Every generation re-verifies itself and fails loudly rather than emitting a fixture that has
drifted from its labels:

- exactly 24 events, ids unique, kinds and precisions valid, week labels are Mondays
- every event id in the ground truth is tagged on at least one real message, and vice versa
- every `source_line_number` renders back to exactly the message the label was derived from
- every `must_mention` appears in the lines it cites
- **no filler line contains a care word** — filler is checked against a ~90-word regex, because
  an accidentally care-shaped filler line would punish a *correct* extractor
- both hazard threads still straddle a simulated chunk boundary
- ~2,000 messages spanning ≥ 175 days

The ground truth is derived from the same Python literal the transcript is rendered from
(`SCRIPT` in `scripts/make_demo_fixture.py`, where each message is tagged with the event it
belongs to), so the two cannot drift apart. **Edit the literal, never the `.txt`.**

Changing the seed or the filler volume moves the chunk boundaries and will fail verification —
that is deliberate, not an obstacle to route around. Re-pick a seed that satisfies it.
