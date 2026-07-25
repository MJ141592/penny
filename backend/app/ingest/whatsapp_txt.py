"""WhatsApp `.txt` export parser. Pure: a file handle in, `InboundMessage`s out.

WHY `dayfirst` AND `tz` ARE REQUIRED ARGUMENTS, NOT SNIFFED

`dd/mm` vs `mm/dd` is decidable only when some timestamp in the file has a first component
greater than 12. An export spanning under 12 days — or one where every message happens to land
in the first 12 days of its month — is *genuinely undecidable from the file*. Guessing wrong is
silent and catastrophic: every date shifts by months, the feed misorders, and every "yesterday"
in extraction anchors to the wrong day, with nothing throwing anywhere.

Timezone is worse. Exports carry no UTC offset at all, so the same chat exported by a daughter
in London and a son in Dubai produces different wall-clock strings and nothing in the file can
tell them apart.

So `sniff()` proposes and the user disposes: the import wizard shows the parsed head and tail
with the proposed settings applied and makes the user confirm. `parse_export()` itself never
guesses — it takes both explicitly and does exactly what it was told.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

from app.ingest.contract import InboundMessage

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TextIO

# Limits. A 40k-message export is WhatsApp's own cap; these sit above it with headroom so a
# legitimate backfill never trips them, and a pasted binary or a zip bomb does.
# 25, not 20: docs/api-contract.md's 413 and the Dockerfile's request timeout are both sized
# for 25 MB, and three places saying 25 while one said 20 is how a 22 MB export gets rejected
# with an error message the contract promises it will not get.
MAX_BYTES = 25 * 1024 * 1024
MAX_MESSAGES = 60_000
# Past this a "message" is a pasted document, not a chat line. Truncate rather than drop: the
# opening words are usually the signal and the tail is boilerplate.
MAX_BODY_CHARS = 8_000

DayfirstEvidence = Literal["day>12", "month>12", "conflict", "none"]

# U+200E/U+200F/U+FEFF are stripped outright; U+202F and U+00A0 become plain spaces. iOS
# prefixes every line and every media placeholder with U+200E, and modern iOS/Android put a
# NARROW NO-BREAK SPACE before AM/PM — which makes strptime's %p fail to match "9:04 PM".
# Skipping this normalisation is the single most common real-world WhatsApp-parser bug.
_INVISIBLE = str.maketrans(
    {
        "\u200e": None,  # LEFT-TO-RIGHT MARK
        "\u200f": None,  # RIGHT-TO-LEFT MARK
        "\ufeff": None,  # ZERO WIDTH NO-BREAK SPACE / BOM
        "\u202f": " ",  # NARROW NO-BREAK SPACE
        "\u00a0": " ",  # NO-BREAK SPACE
    }
)


def normalise_line(raw: str) -> str:
    """Strip the invisibles BEFORE any regex sees the line. Everything else depends on this."""
    return raw.translate(_INVISIBLE).rstrip("\r\n")


# iOS: "[14/07/2026, 21:04:11] Sarah: text"   Android: "14/07/2026, 21:04 - Sarah: text"
_START = re.compile(
    r"^(?:\[(?P<ts_b>[^\]]{6,40})\]\s*"
    r"|(?P<ts_d>\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:\s?[APap]\.?[Mm]\.?)?)\s-\s)"
)
# Looks like a start line but did not match one: a truncated or corrupted export row.
_NEAR_START = re.compile(r"^\[?\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}[,\s]")
# "~ Jane" is the newer marker for a sender who isn't in your contacts.
_SENDER = re.compile(r"^(?P<sender>~?[^:\n]{1,80}?):\s(?P<body>.*)$", re.DOTALL)
_TIME_PART = re.compile(r"\d{1,2}:\d{2}")


def _time_formats(sample: str) -> list[str]:
    """Time formats worth trying, narrowed by whether the sample looks 12h or 24h."""
    has_seconds = sample.count(":") >= 2
    is_12h = re.search(r"[APap]\.?[Mm]", sample) is not None
    if is_12h:
        return ["%I:%M:%S %p"] if has_seconds else ["%I:%M %p"]
    return ["%H:%M:%S"] if has_seconds else ["%H:%M"]


def _date_formats(sample: str, dayfirst: bool) -> list[str]:
    """Date formats worth trying, filtered by the caller's dayfirst decision.

    Filtering here rather than after parsing is the whole point: an unfiltered candidate list
    would happily lock onto %m/%d for a dd/mm file whenever the first 50 rows all have day<=12.
    """
    separator = next((s for s in "/.-" if s in sample), "/")
    head, _, rest = sample.partition(separator)
    year_last = "%Y" if len(rest.rsplit(separator, 1)[-1]) == 4 else "%y"
    if len(head) == 4:  # ISO-ish "2026/07/14" — unambiguous, dayfirst does not apply
        return [f"%Y{separator}%m{separator}%d"]
    first, second = ("%d", "%m") if dayfirst else ("%m", "%d")
    return [f"{first}{separator}{second}{separator}{year_last}"]


def _candidate_formats(sample: str, dayfirst: bool) -> list[str]:
    """Full strptime candidates for one observed timestamp shape."""
    date_part, _, time_part = sample.partition(",")
    if not time_part:  # some exports use a bare space instead of ", "
        date_part, _, time_part = sample.partition(" ")
        joiner = " "
    else:
        joiner = ", "
    date_part, time_part = date_part.strip(), time_part.strip()
    return [
        f"{d}{joiner}{t}"
        for d in _date_formats(date_part, dayfirst)
        for t in _time_formats(time_part)
    ]


def _lock_format(samples: list[str], dayfirst: bool) -> str:
    """Lock onto the first candidate that parses 100% of the first 50 timestamps.

    One strptime call per message for a whole file; `dateparser` would be ~1 ms each — 40 s of
    CPU on a 40k export — and its fuzzy heuristics reintroduce exactly the locale ambiguity the
    explicit `dayfirst` argument just removed.
    """
    seen: list[str] = []
    for sample in samples:
        for candidate in _candidate_formats(sample, dayfirst):
            if candidate not in seen:
                seen.append(candidate)
    for candidate in seen:
        if all(_try_strptime(s, candidate) is not None for s in samples):
            return candidate
    return seen[0] if seen else ""


def _try_strptime(sample: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(sample, fmt)
    except ValueError:
        return None


def _to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Local wall clock -> UTC, taking the earlier reading of a DST fold.

    fold=0 means an ambiguous 01:30 on a clocks-back night resolves to the first (BST) pass, and
    a nonexistent time in the spring gap resolves forward. A one-hour error twice a year on a
    care history is acceptable; a crash on import is not.
    """
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


# Tier 2 system lines: a sender IS present but the body is a group event, not something a human
# typed. Anchored so "Mum added salt to everything" stays a real message.
_SYSTEM_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subject_changed", re.compile(r"^changed the subject (from|to)\b", re.I)),
    ("icon_changed", re.compile(r"^changed th(is|e) group'?s? icon\b", re.I)),
    ("icon_changed", re.compile(r"^changed the group (icon|description|settings)\b", re.I)),
    ("member_added", re.compile(r"^added .{1,80}$", re.I)),
    ("member_left", re.compile(r"^left$", re.I)),
    ("member_removed", re.compile(r"^removed .{1,80}$", re.I)),
    ("member_joined", re.compile(r"^joined using this group'?s? invite link$", re.I)),
    ("phone_changed", re.compile(r"^changed (their|to a new) phone number\b", re.I)),
    ("message_deleted", re.compile(r"^This message was deleted\.?$", re.I)),
    ("message_deleted", re.compile(r"^You deleted this message\.?$", re.I)),
    ("missed_call", re.compile(r"^Missed (voice|video|group) call\b", re.I)),
    ("missed_call", re.compile(r"^(Voice|Video) call, .{1,40}$", re.I)),
    ("pinned", re.compile(r"^pinned a message\b", re.I)),
    ("disappearing", re.compile(r"^turned (on|off) disappearing messages\b", re.I)),
    ("disappearing", re.compile(r"^changed the message timer\b", re.I)),
    ("security_code", re.compile(r"^Your security code with .{1,80} changed\b", re.I)),
    ("admin_changed", re.compile(r"^(is|are) now an admin\b", re.I)),
    ("group_created", re.compile(r"^created (this )?group\b", re.I)),
    ("encryption", re.compile(r"^Messages and calls are end-to-end encrypted\b", re.I)),
    ("waiting", re.compile(r"^Waiting for this message\b", re.I)),
)


def classify_system_phrase(body: str) -> str | None:
    """The system_kind for a group-event body, or None if a human wrote it."""
    stripped = body.strip()
    for kind, pattern in _SYSTEM_PHRASES:
        if pattern.match(stripped):
            return kind
    return None


# Tier 1: a timestamp matched but no "Sender: " did.
_STRUCTURAL_SYSTEM: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("encryption", re.compile(r"end-to-end encrypted", re.I)),
    ("group_created", re.compile(r"\bcreated (this )?group\b", re.I)),
    ("member_joined", re.compile(r"joined using this group'?s? invite link", re.I)),
    ("member_added", re.compile(r"\badded\b", re.I)),
    ("member_removed", re.compile(r"\bremoved\b", re.I)),
    ("member_left", re.compile(r"\bleft\b", re.I)),
    ("subject_changed", re.compile(r"changed the subject", re.I)),
    ("security_code", re.compile(r"security code", re.I)),
    ("disappearing", re.compile(r"disappearing messages", re.I)),
)


def _classify_structural(body: str) -> str:
    for kind, pattern in _STRUCTURAL_SYSTEM:
        if pattern.search(body):
            return kind
    return "unknown_system"


# Media. The message_type values are what the feed renders and what extraction is told was
# shared: "here's the discharge letter [document: ...]" is a strong appointment signal, while
# "look at this [photo]" must produce nothing. v1 never downloads the file.
_ATTACHED = re.compile(r"<attached:\s*(?P<filename>[^>]+)>", re.I)
_FILE_ATTACHED = re.compile(r"(?P<filename>\S+\.\w{2,5})\s*\(file attached\)", re.I)
_OMITTED = re.compile(
    # Anchored to the WHOLE body: an "omitted" placeholder is never accompanied by a caption, so
    # "the video omitted from the discharge letter" is prose, not an attachment. The angle
    # brackets belong to the placeholder — "<Media omitted>" must not leave "<>" as the caption.
    r"^<?\s*(?P<kind>image|video|audio|sticker|document|GIF|Contact card|Media)\s+omitted\s*>?$",
    re.I,
)
_LOCATION = re.compile(r"^(?:Location:|📍)\s*https?://\S*(?:maps|goo\.gl)\S*", re.I)
_LIVE_LOCATION = re.compile(r"^live location shared\b", re.I)

_FILENAME_INFIX = (
    ("-PHOTO-", "image"),
    ("-VIDEO-", "video"),
    ("-AUDIO-", "audio"),
    ("-STICKER-", "sticker"),
    ("-GIF-", "video"),
    ("-DOCUMENT-", "document"),
)
_EXTENSION_TYPE = {
    "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"),
    "png": ("image", "image/png"),
    "webp": ("sticker", "image/webp"),
    "gif": ("video", "image/gif"),
    "mp4": ("video", "video/mp4"),
    "mov": ("video", "video/quicktime"),
    "opus": ("audio", "audio/ogg"),
    "ogg": ("audio", "audio/ogg"),
    "m4a": ("audio", "audio/mp4"),
    "mp3": ("audio", "audio/mpeg"),
    "pdf": ("document", "application/pdf"),
    "docx": ("document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "vcf": ("contact", "text/vcard"),
}
_OMITTED_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "sticker": "sticker",
    "document": "document",
    "gif": "video",
    "contact card": "contact",
    "media": "unknown",  # "<Media omitted>" is deliberately untyped by WhatsApp
}


@dataclass(frozen=True, slots=True)
class Attachment:
    """What a media placeholder told us. Never the file itself — v1 stores no bytes."""

    message_type: str
    filename: str | None = None
    mime_hint: str | None = None


def _type_from_filename(filename: str) -> tuple[str, str | None]:
    upper = filename.upper()
    for infix, kind in _FILENAME_INFIX:
        if infix in upper:
            extension = filename.rsplit(".", 1)[-1].lower()
            return kind, _EXTENSION_TYPE.get(extension, (kind, None))[1]
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSION_TYPE.get(extension, ("unknown", None))


def detect_media(body: str) -> tuple[Attachment, str] | None:
    """Split a body into its attachment and the caption that survives, or None if it's text."""
    stripped = body.strip()
    for pattern in (_ATTACHED, _FILE_ATTACHED):
        match = pattern.search(body)
        if match:
            filename = match.group("filename").strip()
            kind, mime = _type_from_filename(filename)
            caption = (body[: match.start()] + body[match.end() :]).strip()
            return Attachment(kind, filename, mime), caption
    omitted = _OMITTED.match(stripped)
    if omitted:
        return Attachment(_OMITTED_TYPE.get(omitted.group("kind").lower(), "unknown")), ""
    if _LOCATION.match(stripped) or _LIVE_LOCATION.match(stripped):
        # The URL stays in `text`: the coordinates are the only content a location message has.
        return Attachment("location"), stripped
    return None


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Both fields are REQUIRED and have no defaults — see the module docstring."""

    tz: ZoneInfo
    dayfirst: bool


@dataclass(frozen=True, slots=True)
class ExportHints:
    """What `sniff()` SUGGESTS. It decides nothing; the import wizard makes the user confirm."""

    dayfirst: bool
    dayfirst_evidence: DayfirstEvidence
    line_style: Literal["ios", "android", "unknown"]
    is_12h: bool
    has_seconds: bool
    sample_timestamps: list[str]  # shown back to the user so they can eyeball the dates

    @property
    def confident(self) -> bool:
        """False means the UI must force an explicit choice instead of pre-selecting."""
        return self.dayfirst_evidence in ("day>12", "month>12")


@dataclass(slots=True)
class ParseReport:
    """Everything the import wizard shows before the user commits. Mutable: built up in one pass.

    `messages` + `continuations` + `system_lines` + `unparsed_lines` == `total_lines`, always.
    That invariant is what makes "52 unparsed" a number a human can act on.
    """

    total_lines: int = 0
    messages: int = 0
    continuations: int = 0
    system_lines: int = 0
    media_placeholders: int = 0
    unparsed_lines: int = 0
    unparsed_samples: list[str] = field(default_factory=list)  # first 10, for the UI
    detected_format: str = ""
    dayfirst_evidence: DayfirstEvidence = "none"
    senders: dict[str, int] = field(default_factory=dict)
    first_sent_at: datetime | None = None
    last_sent_at: datetime | None = None
    preview_head: list[InboundMessage] = field(default_factory=list)
    preview_tail: list[InboundMessage] = field(default_factory=list)


class ExportTooLargeError(ValueError):
    """Over a hard limit. Specific message so the UI can render it verbatim."""


PREVIEW_SIZE = 5
_MAX_UNPARSED_SAMPLES = 10
_FORMAT_SAMPLE_SIZE = 50


@dataclass(slots=True)
class _Record:
    """One message-in-progress: the start line plus any continuation lines."""

    ordinal: int
    timestamp_text: str
    sender: str | None
    body: str
    raw_lines: list[str]
    truncated: bool = False


def _split_start(line: str) -> tuple[str, str] | None:
    """(timestamp_text, remainder) for a message-start line, else None."""
    match = _START.match(line)
    if not match:
        return None
    timestamp_text = match.group("ts_b") or match.group("ts_d")
    if not _TIME_PART.search(timestamp_text):
        return None
    return timestamp_text.strip(), line[match.end() :]


def _dayfirst_evidence(timestamps: Iterable[str]) -> DayfirstEvidence:
    """Which component is provably the day, judged over every timestamp we saw.

    "none" is the honest and common answer: an export spanning under 12 days simply does not
    contain the evidence, which is exactly why the user has to choose.
    """
    first_over_12 = second_over_12 = False
    for text in timestamps:
        parts = re.split(r"[/.\-]", text.partition(",")[0].strip())
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if len(parts[0]) == 4:  # year-first is unambiguous and proves nothing about dd vs mm
            continue
        first_over_12 |= int(parts[0]) > 12
        second_over_12 |= int(parts[1]) > 12
    if first_over_12 and second_over_12:
        return "conflict"
    if first_over_12:
        return "day>12"
    if second_over_12:
        return "month>12"
    return "none"


def sniff(text_head: str) -> ExportHints:
    """Propose settings from the first slice of a file. Suggests; never decides."""
    timestamps: list[str] = []
    style: Literal["ios", "android", "unknown"] = "unknown"
    for raw in text_head.splitlines():
        line = normalise_line(raw)
        match = _START.match(line)
        if not match or not _TIME_PART.search(match.group("ts_b") or match.group("ts_d")):
            continue
        if style == "unknown":
            style = "ios" if match.group("ts_b") else "android"
        timestamps.append((match.group("ts_b") or match.group("ts_d")).strip())

    evidence = _dayfirst_evidence(timestamps)
    sample = timestamps[0] if timestamps else ""
    time_part = sample.partition(",")[2].strip() or sample
    return ExportHints(
        # month>12 is the only positive proof of mm/dd; everything else defaults to dayfirst,
        # which is right for the UK/EU households this is built for — and the UI still asks.
        dayfirst=evidence != "month>12",
        dayfirst_evidence=evidence,
        line_style=style,
        is_12h=re.search(r"[APap]\.?[Mm]", time_part) is not None,
        has_seconds=time_part.count(":") >= 2,
        sample_timestamps=timestamps[:PREVIEW_SIZE],
    )


def _read_records(fp: TextIO, report: ParseReport) -> list[_Record]:
    """Stream the file into message records, one line at a time.

    Never `fp.read()`: a 25 MB export read whole, decoded, and split costs three copies of the
    file in RAM inside a web process that is also serving requests.
    """
    records: list[_Record] = []
    total_bytes = 0
    for index, raw in enumerate(fp):
        raw = raw.rstrip("\r\n")
        if index == 0:
            raw = raw.lstrip("\ufeff")
        # Postgres `text` and `jsonb` both refuse U+0000, and `raw` is kept verbatim in
        # `payload["raw"]`. A file opened with errors="replace" survives any garbage except
        # this one character, so dropping it here is what keeps "a wrong file was uploaded"
        # a parse report instead of an INSERT that fails after 20,000 rows.
        if "\x00" in raw:
            raw = raw.replace("\x00", "")
        total_bytes += len(raw.encode("utf-8", "replace")) + 1
        if total_bytes > MAX_BYTES:
            raise ExportTooLargeError(f"That export is larger than {MAX_BYTES // 1024 // 1024} MB.")
        report.total_lines += 1
        line = normalise_line(raw)

        start = _split_start(line)
        if start is None:
            _append_continuation(records, raw, line, report)
            continue

        if len(records) >= MAX_MESSAGES:
            raise ExportTooLargeError(f"That export has more than {MAX_MESSAGES:,} messages.")
        timestamp_text, remainder = start
        sender_match = _SENDER.match(remainder)
        sender = sender_match.group("sender").lstrip("~").strip() if sender_match else None
        body = sender_match.group("body") if sender_match else remainder
        records.append(
            _Record(
                ordinal=len(records),
                timestamp_text=timestamp_text,
                sender=sender or None,
                body=body[:MAX_BODY_CHARS],
                raw_lines=[raw],
                truncated=len(body) > MAX_BODY_CHARS,
            )
        )
    return records


def _append_continuation(records: list[_Record], raw: str, line: str, report: ParseReport) -> None:
    """A line that isn't a message start belongs to the message above it.

    With no message above it — a header line, or junk before the first timestamp — it is
    unparsed, which is the honest answer rather than inventing a message to hang it on.
    """
    if not records or _NEAR_START.match(line):
        # A near-miss start line — "17/03/2026, 09:1 - broken line" — is a corrupted message,
        # not prose. Appending it to the message above would silently mangle that message's
        # text; reporting it unparsed puts the damage in front of the user instead.
        _record_unparsed(report, line)
        return
    current = records[-1]
    combined = f"{current.body}\n{line}"
    if len(combined) > MAX_BODY_CHARS:
        # Past the cap this is a pasted document, not a conversation. Keep the head — that is
        # where the signal is — and count the overflow so every line is still accounted for.
        current.body = combined[:MAX_BODY_CHARS]
        if not current.truncated:
            current.truncated = True
            _record_unparsed(report, line)
        else:
            report.unparsed_lines += 1
        return
    current.body = f"{current.body}\n{line}"
    current.raw_lines.append(raw)
    report.continuations += 1


def _record_unparsed(report: ParseReport, line: str) -> None:
    report.unparsed_lines += 1
    if len(report.unparsed_samples) < _MAX_UNPARSED_SAMPLES:
        report.unparsed_samples.append(line)


def _parse_timestamp(text: str, fmt: str, dayfirst: bool) -> datetime | None:
    """The locked format, then dateutil for the occasional straggler line."""
    parsed = _try_strptime(text, fmt) if fmt else None
    if parsed is not None:
        return parsed
    try:
        return dateutil_parser.parse(text, dayfirst=dayfirst, fuzzy=False)
    except (ValueError, OverflowError):
        return None


def _build_message(record: _Record, sent_at: datetime) -> InboundMessage:
    """One record -> one `InboundMessage`, with the raw line(s) kept verbatim in the payload."""
    payload: dict[str, Any] = {"raw": "\n".join(record.raw_lines)}
    if record.truncated:
        payload["truncated"] = True

    system_kind = (
        classify_system_phrase(record.body.split("\n", 1)[0])
        if record.sender
        else _classify_structural(record.body)
    )
    if system_kind:
        payload["system_kind"] = system_kind
        return _inbound(record, sent_at, "system", record.body, payload)

    media = detect_media(record.body)
    if media is None:
        return _inbound(record, sent_at, "text", record.body, payload)

    attachment, caption = media
    if attachment.filename:
        payload["filename"] = attachment.filename
    if attachment.mime_hint:
        payload["mime_hint"] = attachment.mime_hint
    return _inbound(record, sent_at, attachment.message_type, caption or None, payload)


def _inbound(
    record: _Record, sent_at: datetime, message_type: str, text: str | None, payload: dict[str, Any]
) -> InboundMessage:
    return InboundMessage(
        provider="whatsapp_export",
        provider_message_id=None,  # exports carry no message id at all
        sender_wa_jid=None,  # nor a JID: an export gives a display name and nothing else
        sender_wa_lid=None,
        sender_display_name=record.sender,
        sent_at=sent_at,
        text=text,
        message_type=message_type,
        payload=payload,
        source_ordinal=record.ordinal,
    )


def parse_export(fp: TextIO, opts: ExportOptions) -> tuple[list[InboundMessage], ParseReport]:
    """Parse a WhatsApp `.txt` export using EXACTLY the timezone and dayfirst it was given.

    Open the file as utf-8 with errors="replace": a mojibake character loses one glyph, while a
    UnicodeDecodeError loses the family's entire history.
    """
    report = ParseReport()
    records = _read_records(fp, report)

    timestamps = [r.timestamp_text for r in records]
    report.dayfirst_evidence = _dayfirst_evidence(timestamps)
    report.detected_format = _lock_format(timestamps[:_FORMAT_SAMPLE_SIZE], opts.dayfirst)

    messages: list[InboundMessage] = []
    for record in records:
        naive = _parse_timestamp(record.timestamp_text, report.detected_format, opts.dayfirst)
        if naive is None:
            # Its continuations were counted as continuations; they are unparsed with it.
            dropped = len(record.raw_lines) - 1
            report.continuations -= dropped
            report.unparsed_lines += dropped
            _record_unparsed(report, record.raw_lines[0])
            continue
        message = _build_message(record, _to_utc(naive, opts.tz))
        messages.append(message)
        if message.message_type == "system":
            report.system_lines += 1
            continue
        report.messages += 1
        if message.message_type not in ("text", "system"):
            report.media_placeholders += 1
        if message.sender_display_name:
            report.senders[message.sender_display_name] = (
                report.senders.get(message.sender_display_name, 0) + 1
            )

    sent_times = [m.sent_at for m in messages]
    report.first_sent_at = min(sent_times, default=None)
    report.last_sent_at = max(sent_times, default=None)
    # Previews skip system lines: the first line of every export is the encryption notice, and
    # "first message: Messages are end-to-end encrypted" tells the user nothing about the dates.
    human = [m for m in messages if m.message_type != "system"]
    report.preview_head = human[:PREVIEW_SIZE]
    report.preview_tail = human[-PREVIEW_SIZE:]
    return messages, report
