"""The `.txt` parser is the only path to real history, so its failures are silent by nature.

A wrong `dayfirst` shifts every date by months and nothing throws; a missed narrow no-break
space drops every message in a 12-hour export. Both are pinned here by exact `InboundMessage`
equality against hand-written fixtures, each of which exercises exactly one hazard.
"""

import io
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.ingest.contract import InboundMessage
from app.ingest.whatsapp_txt import (
    MAX_BODY_CHARS,
    ExportOptions,
    ExportTooLargeError,
    ParseReport,
    parse_export,
    sniff,
)

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
NNBSP = "\u202f"
LRM = "\u200e"

ALL_FIXTURES = sorted(p.name for p in EXPORTS.glob("*.txt"))


def parse_fixture(
    name: str, *, dayfirst: bool = True, tz: ZoneInfo = LONDON
) -> tuple[list[InboundMessage], ParseReport]:
    with (EXPORTS / name).open(encoding="utf-8", errors="replace") as fp:
        return parse_export(fp, ExportOptions(tz=tz, dayfirst=dayfirst))


def parse_text(
    text: str, *, dayfirst: bool = True, tz: ZoneInfo = LONDON
) -> tuple[list[InboundMessage], ParseReport]:
    return parse_export(io.StringIO(text), ExportOptions(tz=tz, dayfirst=dayfirst))


def message(
    ordinal: int,
    sent_at: datetime,
    sender: str | None,
    text: str | None,
    raw: str,
    message_type: str = "text",
    **payload: object,
) -> InboundMessage:
    """An export message, spelled out in full — these are compared with `==`, not field by field."""
    return InboundMessage(
        provider="whatsapp_export",
        provider_message_id=None,
        sender_wa_jid=None,
        sender_wa_lid=None,
        sender_display_name=sender,
        sent_at=sent_at,
        text=text,
        message_type=message_type,
        payload={"raw": raw, **payload},
        source_ordinal=ordinal,
    )


def utc(month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


def summarise(messages: list[InboundMessage]) -> list[tuple]:
    """Everything except the verbatim payload, which has its own invariant test."""
    return [
        (
            m.source_ordinal,
            m.sent_at,
            m.sender_display_name,
            m.message_type,
            m.text,
            {k: v for k, v in m.payload.items() if k != "raw"},
        )
        for m in messages
    ]


# --- the two flagship fixtures, compared as whole InboundMessages ------------------------------


def test_ios_24h_uk_parses_exactly() -> None:
    messages, report = parse_fixture("ios_24h_uk.txt")
    assert messages == [
        message(
            0,
            utc(7, 14, 19, 11, 4),
            "Sarah",
            "Morning all, Mum slept badly again",
            "[14/07/2026, 20:11:04] Sarah: Morning all, Mum slept badly again",
        ),
        message(
            1,
            utc(7, 14, 19, 12, 31),
            "Tom",
            "How many times was she up?",
            "[14/07/2026, 20:12:31] Tom: How many times was she up?",
        ),
        message(
            2,
            utc(7, 14, 19, 13, 2),
            "Sarah",
            "Four, I think",
            "[14/07/2026, 20:13:02] Sarah: Four, I think",
        ),
        message(
            3,
            utc(7, 15, 8, 4),
            "Priya",
            "GP has a slot Thursday 9:30 with Dr Aziz",
            "[15/07/2026, 09:04:00] Priya: GP has a slot Thursday 9:30 with Dr Aziz",
        ),
        message(
            4,
            utc(7, 15, 8, 5, 47),
            "Sarah",
            "Book it, I can take her",
            "[15/07/2026, 09:05:47] Sarah: Book it, I can take her",
        ),
        message(
            5,
            utc(7, 16, 20, 47, 12),
            "Tom",
            "Night",
            "[16/07/2026, 21:47:12] Tom: Night",
        ),
    ]
    assert report.detected_format == "%d/%m/%Y, %H:%M:%S"
    assert report.senders == {"Sarah": 3, "Tom": 2, "Priya": 1}
    assert (report.first_sent_at, report.last_sent_at) == (
        utc(7, 14, 19, 11, 4),
        utc(7, 16, 20, 47, 12),
    )


def test_android_dash_parses_exactly_and_strips_the_tilde_marker() -> None:
    messages, report = parse_fixture("android_dash.txt")
    assert messages == [
        message(
            0, utc(7, 14, 19, 11), "Sarah", "Morning all", "14/07/2026, 20:11 - Sarah: Morning all"
        ),
        message(
            1,
            utc(7, 14, 19, 12),
            "Tom",
            "How many times was she up?",
            "14/07/2026, 20:12 - Tom: How many times was she up?",
        ),
        # "~ Jane" marks a sender who isn't in the exporter's contacts; the name is still Jane.
        message(
            2,
            utc(7, 15, 8, 4),
            "Jane",
            "I can do Thursday",
            "15/07/2026, 09:04 - ~ Jane: I can do Thursday",
        ),
        message(
            3, utc(7, 15, 8, 5), "Sarah", "Thanks Jane", "15/07/2026, 09:05 - Sarah: Thanks Jane"
        ),
        message(4, utc(7, 16, 20, 47), "Tom", "Night", "16/07/2026, 21:47 - Tom: Night"),
    ]
    assert report.detected_format == "%d/%m/%Y, %H:%M"


# --- normalisation: the invisible characters that break every naive parser --------------------


def test_narrow_no_break_space_before_am_pm_still_parses() -> None:
    # U+202F before AM/PM makes strptime's %p fail to match, which drops EVERY message in a
    # modern 12-hour export. This fixture is written with the real character.
    messages, report = parse_fixture("ios_12h_us_narrow_nbsp.txt", dayfirst=False)
    assert report.detected_format == "%m/%d/%Y, %I:%M:%S %p"
    assert report.unparsed_lines == 0
    assert [m.sent_at for m in messages] == [
        utc(7, 14, 19, 11, 4),
        utc(7, 14, 19, 12, 31),
        utc(7, 15, 8, 4),
        utc(7, 15, 11, 30, 9),
        utc(7, 15, 23, 5),  # 16 July 00:05 local is still 15 July in UTC
    ]


def test_left_to_right_marks_are_stripped_before_matching() -> None:
    messages, report = parse_fixture("ios_lrm_prefixed.txt")
    assert report.unparsed_lines == 0
    assert summarise(messages) == [
        (0, utc(7, 14, 19, 11, 4), "Sarah", "text", "Morning all", {}),
        (
            1,
            utc(7, 14, 19, 12),
            "Tom",
            "image",
            None,
            {
                "filename": "00000042-PHOTO-2026-07-14-20-12-00.jpg",
                "mime_hint": "image/jpeg",
            },
        ),
        (2, utc(7, 14, 19, 13, 2), "Sarah", "text", "Is that her ankle?", {}),
        (3, utc(7, 14, 19, 14, 10), "Tom", "image", None, {}),
    ]
    # The LRM survives in the payload: `raw` is verbatim, not normalised.
    assert LRM in messages[1].payload["raw"]


def test_emoji_and_rtl_names_survive_intact() -> None:
    messages, _ = parse_fixture("emoji_rtl.txt")
    assert [m.sender_display_name for m in messages] == ["Sarah", "مريم", "Tom", "Sarah"]
    assert messages[0].text == "On my way \U0001f697\U0001f4a8"
    assert messages[2].text == "\U0001f44d\U0001f3fd"
    # The RTL mark inside the body is normalised away in `text` but kept in `raw`.
    assert messages[3].text == "Mum said “I'm fine” \U0001f644"
    assert LRM in messages[3].payload["raw"]


# --- dates: separators, two-digit years, and the straggler fallback ----------------------------


def test_two_digit_years_and_dot_separators() -> None:
    messages, report = parse_fixture("android_2digit_year.txt")
    assert report.detected_format == "%d/%m/%y, %H:%M"
    # Line 2 uses dots, so the locked format misses it and dateutil picks it up — the fallback
    # exists for exactly this kind of straggler, never for the whole file.
    assert report.unparsed_lines == 0
    assert [m.sent_at for m in messages] == [
        utc(7, 14, 19, 11),
        utc(7, 14, 19, 12),
        utc(7, 15, 8, 4),
    ]


def test_a_line_whose_timestamp_never_parses_is_counted_not_crashed() -> None:
    messages, report = parse_text(
        "14/07/2026, 20:11 - Sarah: Morning all\n"
        "31/31/2026, 09:1 - broken line\n"
        "14/07/2026, 20:12 - Tom: Night\n"
    )
    assert [m.text for m in messages] == ["Morning all", "Night"]
    # It looked like a start line, so it is not a continuation of "Morning all" either.
    assert report.unparsed_lines == 1
    assert report.unparsed_samples == ["31/31/2026, 09:1 - broken line"]


def test_timezone_is_applied_exactly_as_given() -> None:
    _, london = parse_fixture("ios_24h_uk.txt", tz=LONDON)
    _, new_york = parse_fixture("ios_24h_uk.txt", tz=NEW_YORK)
    assert london.first_sent_at == utc(7, 14, 19, 11, 4)
    assert new_york.first_sent_at == utc(7, 15, 0, 11, 4)


def test_dst_fold_takes_the_earlier_reading_and_the_gap_does_not_crash() -> None:
    # 25 Oct 2026 01:30 happens twice in London and 29 Mar 2026 01:30 never happens. Both must
    # produce a timestamp: a one-hour error twice a year is acceptable, an import crash is not.
    messages, report = parse_text(
        "25/10/2026, 01:30 - Sarah: ambiguous, clocks go back at 02:00\n"
        "29/03/2026, 01:30 - Tom: nonexistent, clocks go forward at 01:00\n"
    )
    assert report.unparsed_lines == 0
    assert [m.sent_at for m in messages] == [
        utc(10, 25, 0, 30),  # fold=0 keeps BST, the earlier of the two passes
        utc(3, 29, 1, 30),
    ]


# --- the undecidable case: dd/mm vs mm/dd ------------------------------------------------------


def test_ambiguous_dates_parse_differently_under_each_dayfirst() -> None:
    # THE reason dayfirst is a required argument. Both readings succeed, neither throws, and the
    # dates differ by months — so the file cannot be allowed to decide.
    day_first, _ = parse_fixture("ambiguous_dates.txt", dayfirst=True)
    month_first, _ = parse_fixture("ambiguous_dates.txt", dayfirst=False)
    assert [m.sent_at for m in day_first] == [
        utc(7, 4, 8, 0),
        utc(7, 5, 8, 0),
        utc(7, 6, 8, 0),
        utc(7, 7, 8, 0),
    ]
    assert [m.sent_at for m in month_first] == [
        utc(4, 7, 8, 0),
        utc(5, 7, 8, 0),
        utc(6, 7, 8, 0),
        utc(7, 7, 8, 0),
    ]
    assert [m.text for m in day_first] == [m.text for m in month_first]


def test_sniff_reports_no_evidence_for_the_ambiguous_export() -> None:
    hints = sniff((EXPORTS / "ambiguous_dates.txt").read_text(encoding="utf-8"))
    assert hints.dayfirst_evidence == "none"
    assert hints.confident is False  # the UI must force an explicit choice


@pytest.mark.parametrize(
    ("fixture", "evidence", "style", "is_12h", "has_seconds"),
    [
        ("ios_24h_uk.txt", "day>12", "ios", False, True),
        ("ios_12h_us_narrow_nbsp.txt", "month>12", "ios", True, True),
        ("android_dash.txt", "day>12", "android", False, False),
        ("android_2digit_year.txt", "day>12", "android", False, False),
        ("ambiguous_dates.txt", "none", "ios", False, True),
    ],
)
def test_sniff_suggests_from_the_head_of_the_file(
    fixture: str, evidence: str, style: str, is_12h: bool, has_seconds: bool
) -> None:
    hints = sniff((EXPORTS / fixture).read_text(encoding="utf-8"))
    assert (hints.dayfirst_evidence, hints.line_style) == (evidence, style)
    assert (hints.is_12h, hints.has_seconds) == (is_12h, has_seconds)
    assert hints.dayfirst is (evidence != "month>12")


def test_sniff_reports_a_conflict_rather_than_picking_a_side() -> None:
    hints = sniff("14/07/2026, 20:11 - Sarah: dd/mm\n07/14/2026, 20:12 - Tom: mm/dd\n")
    assert hints.dayfirst_evidence == "conflict"
    assert hints.confident is False


def test_sniff_on_a_file_with_no_messages_decides_nothing() -> None:
    hints = sniff("this is not a WhatsApp export\n")
    assert hints.dayfirst_evidence == "none"
    assert hints.line_style == "unknown"
    assert hints.sample_timestamps == []


# --- continuations ------------------------------------------------------------------------------


def test_multiline_bodies_are_joined_onto_the_message_above() -> None:
    messages, report = parse_fixture("multiline_bodies.txt")
    assert [m.text for m in messages] == [
        "Shopping list for Mum:\n- milk\n- bread\n- her tablets",
        "Got it",
        "Notes from the GP\n\nBP 140/85, review in 2 weeks",
    ]
    assert (report.messages, report.continuations, report.unparsed_lines) == (3, 5, 0)
    assert messages[0].payload["raw"].splitlines()[1] == "- milk"


def test_a_continuation_with_no_message_above_it_is_unparsed() -> None:
    # Header junk before the first timestamp must not invent a message to hang itself on.
    messages, report = parse_text(
        "Chat export, generated by something else\n14/07/2026, 20:11 - Sarah: Morning all\n"
    )
    assert [m.text for m in messages] == ["Morning all"]
    assert report.unparsed_samples == ["Chat export, generated by something else"]


def test_an_oversized_body_is_truncated_and_reported() -> None:
    messages, report = parse_text(
        "14/07/2026, 20:11 - Sarah: start\n" + "x" * (MAX_BODY_CHARS + 500) + "\n"
    )
    assert len(messages[0].text) == MAX_BODY_CHARS
    assert messages[0].payload["truncated"] is True
    assert report.unparsed_samples == ["x" * (MAX_BODY_CHARS + 500)]


# --- media --------------------------------------------------------------------------------------


def test_attached_media_types_come_from_the_filename() -> None:
    messages, report = parse_fixture("media_attached.txt")
    assert summarise(messages) == [
        (
            0,
            utc(7, 14, 9, 22, 31),
            "Sarah",
            "image",
            None,
            {"filename": "00000042-PHOTO-2026-07-14-10-22-31.jpg", "mime_hint": "image/jpeg"},
        ),
        (
            1,
            utc(7, 14, 9, 23),
            "Sarah",
            "audio",
            None,
            {"filename": "00000043-AUDIO-2026-07-14-10-23-00.opus", "mime_hint": "audio/ogg"},
        ),
        # The caption around the placeholder survives — "here's the discharge letter" is a
        # strong appointment signal, while the file itself is never downloaded.
        (
            2,
            utc(7, 14, 9, 24),
            "Tom",
            "document",
            "here's the discharge letter",
            {"filename": "discharge-letter.pdf", "mime_hint": "application/pdf"},
        ),
        (
            3,
            utc(7, 14, 9, 25),
            "Tom",
            "video",
            None,
            {"filename": "00000044-VIDEO-2026-07-14-10-25-00.mp4", "mime_hint": "video/mp4"},
        ),
        (
            4,
            utc(7, 14, 9, 26),
            "Priya",
            "image",
            None,
            {"filename": "IMG-20260714-WA0002.jpg", "mime_hint": "image/jpeg"},
        ),
    ]
    assert report.media_placeholders == 5


def test_omitted_media_maps_to_typed_kinds() -> None:
    messages, report = parse_fixture("media_omitted.txt")
    assert [(m.message_type, m.text) for m in messages] == [
        ("unknown", None),  # "<Media omitted>" is deliberately untyped by WhatsApp itself
        ("image", None),
        ("video", None),
        ("audio", None),
        ("sticker", None),
        ("document", None),
        ("contact", None),
        ("location", "Location: https://maps.google.com/?q=51.5074,-0.1278"),
    ]
    assert report.media_placeholders == 8
    assert all(m.payload.keys() == {"raw"} for m in messages)


# --- system lines --------------------------------------------------------------------------------


def test_system_lines_are_emitted_with_a_kind_and_keep_the_ordinals_honest() -> None:
    messages, report = parse_fixture("system_lines.txt")
    assert [(m.message_type, m.payload.get("system_kind")) for m in messages] == [
        ("system", "encryption"),  # structural: a timestamp but no "Sender: "
        ("system", "group_created"),
        ("system", "member_added"),
        ("system", "member_added"),
        ("text", None),
        ("system", "message_deleted"),
        ("system", "subject_changed"),
        ("system", "missed_call"),
        ("system", "member_joined"),  # structural again: "+44 ... joined using this group's..."
        ("system", "member_left"),
        # Anchored patterns only: "Mum added salt to everything again" is a real message.
        ("text", None),
    ]
    assert (report.messages, report.system_lines) == (2, 9)
    # System messages are stored, so ordinals stay in step with the file.
    assert [m.source_ordinal for m in messages] == list(range(11))
    # ...and they are excluded from the sender counts the UI shows.
    assert report.senders == {"Sarah": 2}


def test_structural_system_lines_have_no_sender() -> None:
    messages, _ = parse_fixture("system_lines.txt")
    assert messages[0].sender_display_name is None
    assert messages[8].sender_display_name is None


# --- limits ---------------------------------------------------------------------------------------


def test_a_file_over_the_byte_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.whatsapp_txt.MAX_BYTES", 200)
    with pytest.raises(ExportTooLargeError, match="larger than"):
        parse_text("14/07/2026, 20:11 - Sarah: " + "x" * 500 + "\n")


def test_a_file_over_the_message_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.whatsapp_txt.MAX_MESSAGES", 2)
    with pytest.raises(ExportTooLargeError, match="more than"):
        parse_text("".join(f"14/07/2026, 20:1{n} - Sarah: hi\n" for n in range(4)))


def test_an_empty_file_parses_to_nothing_rather_than_failing() -> None:
    messages, report = parse_text("")
    assert messages == []
    assert (report.total_lines, report.first_sent_at, report.preview_head) == (0, None, [])


# --- invariants that must hold for every fixture ---------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_every_line_is_accounted_for(fixture: str) -> None:
    # The partition is what makes "52 unparsed lines" a number a human can act on.
    _, report = parse_fixture(fixture)
    assert (
        report.messages + report.continuations + report.system_lines + report.unparsed_lines
        == report.total_lines
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_payload_keeps_the_source_lines_verbatim(fixture: str) -> None:
    # Re-extraction reads messages.payload, so the raw lines must survive normalisation.
    messages, report = parse_fixture(fixture)
    assert report.unparsed_lines == 0, "fixture is meant to parse cleanly"
    reconstructed = [line for m in messages for line in m.payload["raw"].split("\n")]
    assert reconstructed == (EXPORTS / fixture).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_ordinals_are_contiguous_and_timestamps_are_utc(fixture: str) -> None:
    messages, _ = parse_fixture(fixture)
    assert [m.source_ordinal for m in messages] == list(range(len(messages)))
    assert all(m.sent_at.tzinfo is UTC for m in messages)
    assert all(m.provider == "whatsapp_export" for m in messages)
    assert all(m.provider_message_id is None for m in messages)


def test_previews_show_real_messages_not_the_encryption_notice() -> None:
    _, report = parse_fixture("system_lines.txt")
    assert [m.text for m in report.preview_head] == [
        "Right, everyone's here",
        "Mum added salt to everything again",
    ]
    assert report.preview_tail == report.preview_head


def test_previews_are_capped_at_five_from_each_end() -> None:
    _, report = parse_text(
        "".join(f"14/07/2026, 20:{n:02d} - Sarah: message {n}\n" for n in range(20))
    )
    assert [m.text for m in report.preview_head] == [f"message {n}" for n in range(5)]
    assert [m.text for m in report.preview_tail] == [f"message {n}" for n in range(15, 20)]


# --- property: render in every supported locale shape, parse it back ---------------------------

# (template, dayfirst). Rendering and re-parsing every shape we claim to support is what catches
# regex bugs a hand-written fixture happens to miss.
LOCALE_TEMPLATES = [
    ("[{d:%d/%m/%Y}, {d:%H:%M:%S}] {sender}: {text}", True),
    ("[{d:%m/%d/%Y}, {d:%H:%M:%S}] {sender}: {text}", False),
    ("[{d:%d/%m/%Y}, {d:%I:%M:%S}" + NNBSP + "{ampm}] {sender}: {text}", True),
    ("[{d:%m/%d/%y}, {d:%I:%M}" + NNBSP + "{ampm}] {sender}: {text}", False),
    ("{d:%d/%m/%Y}, {d:%H:%M} - {sender}: {text}", True),
    ("{d:%m/%d/%Y}, {d:%H:%M} - {sender}: {text}", False),
    ("{d:%d/%m/%y}, {d:%H:%M} - {sender}: {text}", True),
    ("{d:%d.%m.%y}, {d:%H:%M} - {sender}: {text}", True),
    ("{d:%d-%m-%Y}, {d:%H:%M:%S} - {sender}: {text}", True),
]

SENDERS = ["Sarah", "Tom", "Priya", "Jane Doe", "Dr Aziz", "مريم"]
# June has no DST transition in London, so a round-trip failure is a parser bug and never a fold.
LOCAL_TIMES = st.datetimes(
    min_value=datetime(2026, 6, 1, 0, 0), max_value=datetime(2026, 6, 30, 23, 59)
).map(lambda d: d.replace(second=0, microsecond=0))
BODY_ALPHABET = st.characters(
    codec="utf-8",
    exclude_characters="\n\r\u200e\u200f\ufeff\u202f\u00a0<>",
    exclude_categories=("Cc", "Cs"),
)
BODIES = (
    st.text(alphabet=BODY_ALPHABET, min_size=1, max_size=60)
    .map(str.strip)
    .filter(lambda t: t and t == t.strip() and "omitted" not in t.lower())
)


@settings(max_examples=200, deadline=None)
@given(
    rows=st.lists(
        st.tuples(LOCAL_TIMES, st.sampled_from(SENDERS), BODIES), min_size=1, max_size=12
    ),
    template_index=st.integers(min_value=0, max_value=len(LOCALE_TEMPLATES) - 1),
)
def test_round_trip_through_every_locale_format(rows: list[tuple], template_index: int) -> None:
    template, dayfirst = LOCALE_TEMPLATES[template_index]
    has_seconds = "%S" in template
    lines = []
    expected = []
    for local, sender, text in rows:
        local = local if has_seconds else local.replace(second=0)
        lines.append(
            template.format(
                d=local, sender=sender, text=text, ampm="AM" if local.hour < 12 else "PM"
            )
        )
        expected.append((local.replace(tzinfo=LONDON).astimezone(UTC), sender, text))

    messages, report = parse_export(
        io.StringIO("\n".join(lines) + "\n"), ExportOptions(tz=LONDON, dayfirst=dayfirst)
    )

    assert report.unparsed_lines == 0
    assert [(m.sent_at, m.sender_display_name, m.text) for m in messages] == expected


def test_prose_that_merely_contains_a_placeholder_phrase_stays_text() -> None:
    # "omitted" placeholders never carry a caption, so the pattern is anchored to the whole body.
    messages, _ = parse_text(
        "14/07/2026, 10:22 - Sarah: the video omitted from the discharge letter was the useful bit\n"
    )
    assert messages[0].message_type == "text"


def test_a_wrong_dayfirst_falls_back_rather_than_dropping_unambiguous_rows() -> None:
    # dayfirst=True over an mm/dd file: "07/14/2026" cannot be a day-first date, so the locked
    # format misses and dateutil resolves the only possible reading. The report still says
    # month>12, which is what the wizard shows the user to explain the mismatch.
    messages, report = parse_fixture("ios_12h_us_narrow_nbsp.txt", dayfirst=True)
    assert report.dayfirst_evidence == "month>12"
    assert report.unparsed_lines == 0
    assert messages[0].sent_at == utc(7, 14, 19, 11, 4)
