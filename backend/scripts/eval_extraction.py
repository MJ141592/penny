"""The M1 go/no-go: extract `fixtures/demo_family.txt` and score it against its 24 labels.

    uv run python -m scripts.eval_extraction                 # real run, costs ~$1
    uv run python -m scripts.eval_extraction --record        # …and save the raw responses
    uv run python -m scripts.eval_extraction --transport fake  # replay a recording, free

THE THRESHOLDS WERE WRITTEN BEFORE THE FIRST RUN AND ARE NOT NEGOTIABLE AFTER SEEING A SCORE:

    >= 20 of 24 events found with the correct date, <= 2 invented events, <= 2 duplicate pairs.

That is the whole point of having them. If the run fails, the prompt, the chunker or the
schema is wrong — softening the bar here just moves the discovery to the first real family.

HOW A PREDICTION IS MATCHED TO A LABEL. Same `kind`, every `must_mention` substring present in
the event's text (case- and whitespace-folded), and a date correct to the granularity the
label claims. Identity and date are scored SEPARATELY: an event found with the wrong date is a
different, much less alarming failure than one not found at all, and collapsing the two hides
which of the prompt's two jobs is broken.

WHAT COUNTS AS INVENTED. A prediction that matched no label AND cites no message the ground
truth points at. The fixture guarantees no filler line contains a care word, so an event built
entirely out of filler is not a debatable judgement call — it is a hallucination.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.extraction.runner import (
    ExtractedEventRecord,
    ExtractionRunResult,
    as_chunk_messages,
    extract_messages,
    local_message_id,
    normalise_precision,
)
from app.ingest.whatsapp_txt import ExportOptions, parse_export
from app.llm.gateway import LLMGateway
from app.llm.transport import FakeTransport, OpenAITransport, get_transport
from app.openai_client import get_openai_client
from scripts.extract_export import render_totals

if TYPE_CHECKING:
    from uuid import UUID

    from openai.types.responses import Response

    from app.ingest.contract import InboundMessage

BACKEND = Path(__file__).resolve().parents[1]
EXPORT = BACKEND / "fixtures" / "demo_family.txt"
GROUND_TRUTH = BACKEND / "fixtures" / "demo_family_events.json"
RECORDINGS = BACKEND / "tests" / "fixtures" / "responses" / "eval"

TZ = ZoneInfo("Europe/London")
DAYFIRST = True

# From the plan's acceptance line for M1. Do not touch these after a run.
MIN_EVENTS_FOUND = 20
MAX_INVENTED = 2
MAX_DUPLICATE_PAIRS = 2

# The fixture's own cast, written as `households.care_brief` will be: the block that turns
# "she" into Margaret and tells the model who is not the care recipient.
CARE_BRIEF = """\
Care recipient: Margaret Doyle, 84, called "Mum", "Nan" and "our Margaret" in this chat.
Lives: her own home in Salford, alone, with carer visits on weekday mornings.
Household members: Sarah (daughter, main coordinator), Tom (son, in Leeds), Priya (Tom's
  wife), Dot (Margaret's sister), Jean (next-door neighbour).
Known conditions the family has mentioned: atrial fibrillation, osteoarthritis of the left
  hip, memory problems in their own words and undiagnosed.
Medications the family has mentioned: apixaban, bisoprolol, co-codamol.
Regular providers: Dr Aziz (GP, Brookfield surgery), Salford Royal (cardiology).
Nobody else in this chat is the care recipient. Priya's dentist, Tom's dog and Dot's knee
are not care events."""


@dataclass(frozen=True, slots=True)
class Label:
    """One of the 24 hand-placed ground-truth events."""

    id: str
    kind: str
    occurred_at: datetime
    precision: str
    title: str
    must_mention: tuple[str, ...]
    source_line_numbers: tuple[int, ...]


@dataclass(slots=True)
class Score:
    labels: list[Label]
    predictions: list[ExtractedEventRecord]
    matched: dict[str, int] = field(default_factory=dict)  # label id -> prediction index
    date_correct: set[str] = field(default_factory=set)
    duplicate_pairs: int = 0
    invented: list[int] = field(default_factory=list)
    unmatched_but_grounded: list[int] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            len(self.date_correct) >= MIN_EVENTS_FOUND
            and len(self.invented) <= MAX_INVENTED
            and self.duplicate_pairs <= MAX_DUPLICATE_PAIRS
        )


def load_labels() -> list[Label]:
    raw = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    return [
        Label(
            id=item["id"],
            kind=item["kind"],
            occurred_at=datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")),
            precision=item["occurred_at_precision"],
            title=item["title"],
            must_mention=tuple(item["must_mention"]),
            source_line_numbers=tuple(item["source_line_numbers"]),
        )
        for item in raw
    ]


def load_export() -> tuple[list[InboundMessage], dict[int, UUID]]:
    """Parse the fixture and map each 1-based file line to the message that occupies it.

    The mapping is what makes "traceable to a real source message" checkable: the labels cite
    line numbers, the events cite message ids, and this is the only place the two meet.
    """
    with EXPORT.open(encoding="utf-8", errors="replace") as handle:
        messages, _ = parse_export(handle, ExportOptions(tz=TZ, dayfirst=DAYFIRST))

    lines = EXPORT.read_text(encoding="utf-8", errors="replace").splitlines()
    by_line: dict[int, UUID] = {}
    cursor = 0
    for message in messages:
        raw = str(message.payload.get("raw", "")).split("\n")
        while cursor < len(lines) and lines[cursor] != raw[0]:
            cursor += 1  # an unparsed line; the fixture has none, but do not silently misalign
        message_id = local_message_id(message)
        for offset in range(len(raw)):
            by_line[cursor + offset + 1] = message_id
        cursor += len(raw)
    return messages, by_line


# --- scoring -------------------------------------------------------------------------


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def searchable(record: ExtractedEventRecord) -> str:
    """Everything a `must_mention` may hide in: title, body, and the kind-specific fields.

    The fixture's README specifies title + body + details, and `dizz` legitimately lives in
    `symptom_name` alone when the title says "Another funny turn".
    """
    event = record.event
    parts = [event.title, event.body or "", event.natural_key]
    parts += [
        event.symptom_name or "",
        event.body_site or "",
        event.duration_text or "",
        event.provider_name or "",
        event.location or "",
        event.appointment_kind or "",
        event.outcome or "",
        event.medication_name or "",
        event.dose_text or "",
        event.medication_action or "",
        event.prescriber or "",
        event.note_category or "",
    ]
    parts += event.attendees + event.follow_up_actions
    return _fold(" ".join(parts))


def _local_day(moment: datetime, precision: str, tz: ZoneInfo) -> date:
    """The calendar day an event belongs to, for both sides of the comparison.

    Only `exact`-precision events are timezone-converted. A `day` event is stored as local
    midnight rendered as `00:00:00Z`; converting it would push it into the previous day during
    BST — the fixture README says the same thing, and both encoders must agree or every summer
    event scores as off-by-one.
    """
    return (
        moment.astimezone(tz).date() if normalise_precision(precision) == "exact" else moment.date()
    )


def date_matches(label: Label, record: ExtractedEventRecord, tz: ZoneInfo) -> bool:
    """Correct to the granularity the LABEL claims — a week label cannot demand a day."""
    if normalise_precision(label.precision) == "unknown":
        return True
    if record.occurred_at_utc is None:
        return False
    want = _local_day(label.occurred_at, label.precision, tz)
    got = _local_day(record.occurred_at_utc, record.event.occurred_at_precision, tz)
    grain = normalise_precision(label.precision)
    if grain == "month":
        return (want.year, want.month) == (got.year, got.month)
    if grain == "week":
        return want.isocalendar()[:2] == got.isocalendar()[:2]
    return want == got


def score(labels: list[Label], result: ExtractionRunResult, by_line: dict[int, UUID]) -> Score:
    predictions = result.events
    outcome = Score(labels=labels, predictions=predictions)

    candidates: dict[str, list[int]] = {}
    for label in labels:
        text_hits = [
            index
            for index, record in enumerate(predictions)
            if record.event.kind == label.kind
            and all(_fold(m) in searchable(record) for m in label.must_mention)
        ]
        candidates[label.id] = text_hits

    claimed: set[int] = set()
    for label in labels:
        free = [i for i in candidates[label.id] if i not in claimed]
        dated = [i for i in free if date_matches(label, predictions[i], TZ)]
        chosen = dated[0] if dated else (free[0] if free else None)
        if chosen is None:
            continue
        claimed.add(chosen)
        outcome.matched[label.id] = chosen
        if dated:
            outcome.date_correct.add(label.id)
        # Anything else that also matches this label on kind, text AND date survived the
        # merge as a duplicate of an event the family would see twice.
        extras = [i for i in dated[1:] if i not in claimed]
        claimed.update(extras)
        outcome.duplicate_pairs += len(extras)

    grounded_ids = {
        by_line[line] for label in labels for line in label.source_line_numbers if line in by_line
    }
    for index, record in enumerate(predictions):
        if index in claimed:
            continue
        if set(record.source_message_ids) & grounded_ids:
            outcome.unmatched_but_grounded.append(index)
        else:
            outcome.invented.append(index)
    return outcome


def per_kind(outcome: Score) -> list[tuple[str, int, int, float, int, int, float]]:
    rows = []
    for kind in ("appointment", "symptom", "medication", "note"):
        labels = [label for label in outcome.labels if label.kind == kind]
        found = sum(1 for label in labels if label.id in outcome.matched)
        predicted = [r for r in outcome.predictions if r.event.kind == kind]
        matched_predictions = sum(
            1 for i in outcome.matched.values() if outcome.predictions[i].event.kind == kind
        )
        recall = found / len(labels) if labels else 0.0
        precision = matched_predictions / len(predicted) if predicted else 0.0
        rows.append(
            (kind, matched_predictions, len(predicted), precision, found, len(labels), recall)
        )
    return rows


def render_score(outcome: Score, result: ExtractionRunResult) -> str:
    lines = ["", "Per kind", "  kind          precision            recall"]
    for kind, mp, tp, precision, found, total, recall in per_kind(outcome):
        lines.append(
            f"  {kind:<12} {mp:>3}/{tp:<3} {precision:>6.1%}  {found:>3}/{total:<3} {recall:>6.1%}"
        )

    lines += ["", "Ground truth"]
    for label in outcome.labels:
        index = outcome.matched.get(label.id)
        if index is None:
            mark, detail = "MISS", ""
        elif label.id in outcome.date_correct:
            mark = "ok  "
            detail = f"  {outcome.predictions[index].event.title}"
        else:
            got = outcome.predictions[index].occurred_at_utc
            mark = "DATE"
            detail = (
                f"  wanted {label.occurred_at:%Y-%m-%d}, got {got:%Y-%m-%d}" if got else "  undated"
            )
        lines.append(f"  [{mark}] {label.id}  {label.kind:<12} {label.title}{detail}")

    if outcome.invented:
        lines += ["", "Invented (no label, no ground-truth source message)"]
        lines += [f"  - {outcome.predictions[i].event.title}" for i in outcome.invented]
    if outcome.unmatched_but_grounded:
        lines += ["", "Extra, but drawn from real care messages (not counted as invented)"]
        lines += [
            f"  - {outcome.predictions[i].event.title}" for i in outcome.unmatched_but_grounded
        ]

    lines += [
        "",
        "M1 acceptance",
        f"  events found with correct date  {len(outcome.date_correct):>3}/24"
        f"   (need >= {MIN_EVENTS_FOUND})",
        f"  invented events                 {len(outcome.invented):>3}     "
        f"   (need <= {MAX_INVENTED})",
        f"  duplicate pairs after merge     {outcome.duplicate_pairs:>3}     "
        f"   (need <= {MAX_DUPLICATE_PAIRS})",
        f"  total cost                      ${result.total_cost_usd:.4f}",
        f"  wall time                       {result.duration_s:.1f}s",
        "",
        "PASS" if outcome.passed else "FAIL",
    ]
    return "\n".join(lines)


# --- transports ----------------------------------------------------------------------


class RecordingTransport:
    """Forwards to the real API and keeps every parsed `Response`, so one paid run can be
    replayed offline for ever after. Only ever wrapped around a deliberate real run."""

    def __init__(self, inner: OpenAITransport) -> None:
        self._inner = inner
        self.recorded: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Response:
        response = await self._inner.create(**kwargs)
        self.recorded.append(response.model_dump(mode="json"))
        return response

    def save(self, directory: Path) -> int:
        directory.mkdir(parents=True, exist_ok=True)
        for existing in directory.glob("*.json"):
            existing.unlink()
        for index, body in enumerate(self.recorded, 1):
            (directory / f"{index:03d}.json").write_text(json.dumps(body, indent=1) + "\n")
        return len(self.recorded)


def replay_transport() -> FakeTransport:
    files = sorted(RECORDINGS.glob("*.json"))
    if not files:
        raise SystemExit(
            f"No recording in {RECORDINGS}. Run once with --record first (that call costs money)."
        )
    return FakeTransport([json.loads(path.read_text()) for path in files])


# --- entry point ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.eval_extraction", description=__doc__)
    parser.add_argument("--transport", choices=("real", "fake"), default="real")
    parser.add_argument("--record", action="store_true", help="save the raw responses for replay")
    parser.add_argument("--max-spend", type=Decimal, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable summary as well")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    labels = load_labels()
    inbound, by_line = load_export()
    messages = as_chunk_messages(inbound)

    recorder: RecordingTransport | None = None
    if args.transport == "fake":
        transport = replay_transport()
    elif args.record:
        recorder = RecordingTransport(OpenAITransport(get_openai_client()))
        transport = recorder
    else:
        transport = get_transport()

    if args.transport == "real" and not settings.openai_api_key:
        print(
            "OPENAI_API_KEY is not set; use --transport fake to replay a recording.",
            file=sys.stderr,
        )
        return 2

    result = asyncio.run(
        extract_messages(
            messages,
            care_brief=CARE_BRIEF,
            tz=TZ,
            gateway=LLMGateway(transport=transport),
            max_spend_usd=args.max_spend
            if args.max_spend is not None
            else settings.import_max_spend_usd,
        )
    )
    if recorder is not None:
        print(f"Recorded {recorder.save(RECORDINGS)} responses to {RECORDINGS}")

    print(render_totals(result))
    outcome = score(labels, result, by_line)
    print(render_score(outcome, result))
    if args.json:
        print(
            json.dumps(
                {
                    "found_with_correct_date": len(outcome.date_correct),
                    "found": len(outcome.matched),
                    "invented": len(outcome.invented),
                    "duplicate_pairs": outcome.duplicate_pairs,
                    "cost_usd": str(result.total_cost_usd),
                    "duration_s": round(result.duration_s, 1),
                    "invalid_handle_rate": round(result.invalid_handle_rate, 4),
                    "passed": outcome.passed,
                }
            )
        )
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
