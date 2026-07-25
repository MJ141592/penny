"""Parse a WhatsApp `.txt` export, extract care events, print the feed and what it cost.

RUN `--dry-run` FIRST. It chunks the file, renders the exact prompts the real run would send,
and prints the chunk count, the token estimate and the projected cost — making zero API calls.
Nobody should discover the price of an import by paying it:

    uv run python -m scripts.extract_export fixtures/demo_family.txt \\
        --dayfirst --tz Europe/London --dry-run

Then, when the number looks right, drop `--dry-run`:

    uv run python -m scripts.extract_export fixtures/demo_family.txt --dayfirst --tz Europe/London

`--dayfirst`/`--no-dayfirst` and `--tz` are REQUIRED and never guessed. `dd/mm` vs `mm/dd` is
undecidable from an export spanning under 12 days, and exports carry no UTC offset at all —
a wrong guess shifts every date by months and nothing throws.

`--max-spend` is a hard ceiling. The run aborts mid-flight and leaves the remaining messages
unextracted, which is exactly what makes it resumable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.extraction.chunker import build_chunks
from app.extraction.runner import (
    EXTRACT_EFFORT,
    EXTRACT_MAX_OUTPUT_TOKENS,
    MAX_CONCURRENT_CHUNKS,
    ExtractionRunResult,
    as_chunk_messages,
    extract_messages,
    render_call_input,
)
from app.ingest.whatsapp_txt import ExportOptions, parse_export
from app.llm.gateway import LLMGateway
from app.llm.pricing import cost_usd
from app.llm.prompts import EXTRACT_PROMPT
from app.llm.schemas import ExtractionResult, to_strict_json_schema

if TYPE_CHECKING:
    from app.extraction.chunker import Chunk, ChunkMessage
    from app.ingest.whatsapp_txt import ParseReport

# ~4 characters per token for English chat. Good to about ±10%, which is the right accuracy
# for a number whose job is to stop someone spending $19 by accident.
CHARS_PER_TOKEN = 4
# From the plan's cost table: ~250 reasoning tokens plus ~3 events at ~170 tokens each.
OUTPUT_TOKENS_PER_CHUNK = 760
# 334 chunks in ~17 minutes at concurrency 4, per the plan's 40k-message backfill row.
SECONDS_PER_CHUNK = 12


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a real run of these chunks would cost, priced from the real prompt text."""

    chunks: int
    messages: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    minutes: int


def parse_file(
    path: Path, *, dayfirst: bool, tz: ZoneInfo
) -> tuple[list[ChunkMessage], ParseReport]:
    """Read an export off disk and adapt it for extraction. errors='replace' on purpose:
    one mojibake glyph is a better outcome than a UnicodeDecodeError losing the whole history."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        messages, report = parse_export(handle, ExportOptions(tz=tz, dayfirst=dayfirst))
    return as_chunk_messages(messages), report


def build_care_brief(report: ParseReport, care_recipient: str) -> str:
    """The block that turns "she" into a name.

    In the product this is `households.care_brief`, written at onboarding and maintained by
    extraction. The CLI has neither, so it synthesises the one fact it can actually observe —
    who talks in this chat — and takes the care recipient's name as an argument.
    """
    senders = sorted(report.senders, key=lambda name: report.senders[name], reverse=True)
    return (
        f"Care recipient: {care_recipient}. Messages about anyone else are not care events "
        "unless that person is the actor.\n"
        f"People in this chat: {', '.join(senders) or 'unknown'}."
    )


def estimate_run(chunks: list[Chunk], *, care_brief: str, tz: ZoneInfo, model: str) -> Estimate:
    """Price the exact prompts `extract_messages` would send, without sending any of them."""
    fixed = _tokens(EXTRACT_PROMPT) + _tokens(json.dumps(to_strict_json_schema(ExtractionResult)))
    input_tokens = sum(
        fixed + _tokens(render_call_input(chunk, care_brief=care_brief, tz=tz)) for chunk in chunks
    )
    output_tokens = OUTPUT_TOKENS_PER_CHUNK * len(chunks)
    seconds = (len(chunks) / MAX_CONCURRENT_CHUNKS) * SECONDS_PER_CHUNK
    return Estimate(
        chunks=len(chunks),
        messages=sum(len(chunk.primary) for chunk in chunks),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, input_tokens, output_tokens),
        minutes=max(1, round(seconds / 60)),
    )


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def render_parse_report(path: Path, report: ParseReport, messages: list[ChunkMessage]) -> str:
    span = ""
    if report.first_sent_at and report.last_sent_at:
        span = f"  {report.first_sent_at:%Y-%m-%d} to {report.last_sent_at:%Y-%m-%d}"
    senders = ", ".join(
        f"{n} {c}" for n, c in sorted(report.senders.items(), key=lambda kv: -kv[1])
    )
    return "\n".join(
        [
            f"{path.name}: {report.total_lines:,} lines -> {report.messages:,} messages"
            f" ({len(messages):,} extractable){span}",
            f"  format {report.detected_format!r}  dayfirst evidence {report.dayfirst_evidence}",
            f"  continuations {report.continuations:,}  system {report.system_lines:,}"
            f"  media {report.media_placeholders:,}  unparsed {report.unparsed_lines:,}",
            f"  senders: {senders}",
        ]
    )


def render_estimate(estimate: Estimate, model: str) -> str:
    return "\n".join(
        [
            "Dry run — no API call was made.",
            f"  chunks           {estimate.chunks}",
            f"  messages         {estimate.messages:,}",
            f"  input tokens     ~{estimate.input_tokens:,}",
            f"  output tokens    ~{estimate.output_tokens:,} "
            f"(at {OUTPUT_TOKENS_PER_CHUNK}/chunk, incl. reasoning)",
            f"  projected cost   ~${estimate.cost_usd:.2f}  ({model},"
            f" effort={EXTRACT_EFFORT}, max_output_tokens={EXTRACT_MAX_OUTPUT_TOKENS})",
            f"  projected time   ~{estimate.minutes} min at concurrency {MAX_CONCURRENT_CHUNKS}",
        ]
    )


def render_feed(result: ExtractionRunResult, tz: ZoneInfo) -> str:
    """The event feed, newest last, the way the family would read it."""
    if not result.events:
        return "No events extracted."
    lines: list[str] = []
    for record in result.events:
        event = record.event
        when = (
            record.occurred_at_utc.astimezone(tz).strftime("%a %d %b %Y %H:%M")
            if record.occurred_at_utc
            else "undated"
        )
        mentions = f"  x{record.mention_count}" if record.mention_count > 1 else ""
        lines.append(f"{when}  [{event.kind}/{record.occurred_at_precision}]{mentions}")
        lines.append(f"    {event.title}")
        if event.body:
            lines.append(f"    {event.body}")
        for excerpt in record.source_excerpts[:2]:
            lines.append(f"      > {excerpt.sender}: {excerpt.quote}")
        lines.append("")
    return "\n".join(lines)


def render_totals(result: ExtractionRunResult) -> str:
    failed = sum(1 for chunk in result.chunks if chunk.status == "failed")
    return "\n".join(
        [
            f"events           {len(result.events)}"
            f"  (merge calls {result.merge_calls}, dropped {sum(result.dropped_events.values())})",
            f"chunks           {result.chunk_count}  ({failed} failed)",
            f"tokens           in {result.input_tokens:,}"
            f"  cached {result.cached_input_tokens:,}"
            f"  out {result.output_tokens:,}  reasoning {result.reasoning_tokens:,}",
            f"cost             ${result.total_cost_usd:.4f}",
            f"invalid handles  {result.invalid_handles}/{result.handles_cited}"
            f" ({result.invalid_handle_rate:.1%})",
            f"messages         {len(result.extracted_message_ids):,} extracted,"
            f" {len(result.unextracted_message_ids):,} left for the next run",
            f"wall clock       {result.duration_s:.1f}s"
            + (f"  ABORTED: {result.aborted_reason}" if result.aborted_reason else ""),
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.extract_export",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", type=Path, help="the WhatsApp .txt export")
    parser.add_argument(
        "--dayfirst",
        action=argparse.BooleanOptionalAction,
        required=True,
        help="dd/mm (--dayfirst) or mm/dd (--no-dayfirst). Never guessed.",
    )
    parser.add_argument("--tz", required=True, help="IANA timezone the export was written in")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="chunk, render and price the run without calling the API",
    )
    parser.add_argument("--max-spend", type=Decimal, default=None, help="hard USD ceiling")
    parser.add_argument("--care-recipient", default="the care recipient")
    parser.add_argument("--care-brief-file", type=Path, default=None)
    parser.add_argument(
        "--limit", type=int, default=None, help="only the first N messages (for a cheap smoke test)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    tz = ZoneInfo(args.tz)
    model = settings.llm_model_extract
    max_spend = args.max_spend if args.max_spend is not None else settings.import_max_spend_usd

    messages, report = parse_file(args.path, dayfirst=args.dayfirst, tz=tz)
    if args.limit:
        messages = messages[: args.limit]
    print(render_parse_report(args.path, report, messages))
    if not messages:
        print("Nothing to extract.", file=sys.stderr)
        return 1

    care_brief = (
        args.care_brief_file.read_text(encoding="utf-8")
        if args.care_brief_file
        else build_care_brief(report, args.care_recipient)
    )
    chunks = build_chunks(messages)
    estimate = estimate_run(chunks, care_brief=care_brief, tz=tz, model=model)
    print()
    print(render_estimate(estimate, model))

    if args.dry_run:
        return 0

    # Mirrors the 409 the import route returns: refuse before spending, not halfway through.
    if estimate.cost_usd > max_spend:
        print(
            f"\nThat run would cost about ${estimate.cost_usd:.2f}, over the ${max_spend} limit."
            " Raise --max-spend or pass --limit.",
            file=sys.stderr,
        )
        return 2
    if not settings.openai_api_key:
        print("\nOPENAI_API_KEY is not set; only --dry-run works.", file=sys.stderr)
        return 2

    result = asyncio.run(
        extract_messages(
            messages,
            care_brief=care_brief,
            tz=tz,
            gateway=LLMGateway(),
            max_spend_usd=max_spend,
        )
    )
    print()
    print(render_feed(result, tz))
    print(render_totals(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
