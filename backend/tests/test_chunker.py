"""Chunk boundaries decide what the model can see at once, and the transcript is the
only thing it ever reads. Both are pure, so both are pinned exactly."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.extraction.chunker import (
    HARD_MAX,
    MAX_SPAN,
    MIN_FOR_GAP_BREAK,
    OVERLAP_MAX_AGE,
    OVERLAP_MESSAGES,
    QUIET_GAP,
    TARGET,
    ChunkMessage,
    build_chunks,
    estimated_chunk_count,
    render_transcript,
)

LONDON = ZoneInfo("Europe/London")
MINUTE = timedelta(minutes=1)
# A Tuesday, 09:00 in London (08:00 UTC — the offset is the point).
START = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def msg(offset: timedelta = timedelta(), **overrides) -> ChunkMessage:
    fields = {
        "id": uuid4(),
        "sent_at": START + offset,
        "sender_display_name": "Sarah",
        "text": "ok",
    }
    return ChunkMessage(**(fields | overrides))


def run(
    count: int, *, start: timedelta = timedelta(), step: timedelta = MINUTE
) -> list[ChunkMessage]:
    return [msg(start + i * step) for i in range(count)]


def after(messages: list[ChunkMessage], gap: timedelta) -> timedelta:
    """Offset for a message arriving `gap` after the last of `messages`."""
    return messages[-1].sent_at - START + gap


def ids(messages) -> list:
    return [m.id for m in messages]


def test_no_messages_yields_no_chunks() -> None:
    assert build_chunks([]) == []


def test_a_short_conversation_is_one_chunk() -> None:
    messages = run(12)
    [chunk] = build_chunks(messages)
    assert ids(chunk.primary) == ids(messages)
    assert chunk.context == ()


def test_a_quiet_gap_below_the_floor_does_not_split() -> None:
    # Under MIN_FOR_GAP_BREAK a chunk is not yet worth the fixed prompt overhead.
    early = run(MIN_FOR_GAP_BREAK - 1)
    late = run(10, start=after(early, QUIET_GAP))
    [chunk] = build_chunks(early + late)
    assert len(chunk.primary) == MIN_FOR_GAP_BREAK + 9


def test_a_quiet_gap_splits_once_the_floor_is_reached() -> None:
    early = run(MIN_FOR_GAP_BREAK)
    late = run(10, start=after(early, QUIET_GAP))
    first, second = build_chunks(early + late)
    assert [len(first.primary), len(second.primary)] == [MIN_FOR_GAP_BREAK, 10]


def test_a_pause_shorter_than_the_quiet_gap_does_not_split() -> None:
    early = run(MIN_FOR_GAP_BREAK + 20)
    late = run(10, start=after(early, QUIET_GAP - MINUTE))
    [chunk] = build_chunks(early + late)
    assert len(chunk.primary) == MIN_FOR_GAP_BREAK + 30


def test_a_dense_burst_hard_closes_at_the_maximum() -> None:
    # No gap ever arrives, so only HARD_MAX can stop the chunk growing.
    assert [len(c.primary) for c in build_chunks(run(HARD_MAX + 50))] == [HARD_MAX, 50]


def test_a_trickle_closes_when_the_span_cap_is_passed() -> None:
    # One message a day never reaches the gap-break floor, so MAX_SPAN is the only brake —
    # and without it "yesterday" in the last message would be read beside March.
    chunks = build_chunks([msg(day * timedelta(days=1)) for day in range(20)])
    assert [len(c.primary) for c in chunks] == [15, 5]
    assert all(c.primary[-1].sent_at - c.primary[0].sent_at <= MAX_SPAN for c in chunks)


def test_no_message_is_primary_in_two_chunks() -> None:
    # This is what makes messages.extracted_at a sound cursor rather than a double bill.
    early = run(HARD_MAX + 30)
    late = run(120, start=after(early, QUIET_GAP))
    messages = early + late
    primary = [m for chunk in build_chunks(messages) for m in chunk.primary]
    assert ids(primary) == ids(messages)
    assert len(set(ids(primary))) == len(messages)


def test_messages_are_walked_in_sent_at_then_ordinal_order() -> None:
    # Export timestamps have no seconds, so within a minute the line number is the order.
    later = msg(5 * MINUTE, source_ordinal=9)
    same_minute = [msg(source_ordinal=ordinal) for ordinal in (3, 1, 2)]
    [chunk] = build_chunks([later, *same_minute])
    assert [m.source_ordinal for m in chunk.primary] == [1, 2, 3, 9]


def test_context_comes_from_already_extracted_messages() -> None:
    history = run(3, start=-30 * MINUTE)
    primary = run(5)
    [chunk] = build_chunks(primary, already_extracted=history)
    assert ids(chunk.context) == ids(history)
    assert ids(chunk.primary) == ids(primary)


def test_context_excludes_anything_older_than_the_overlap_window() -> None:
    stale = msg(-OVERLAP_MAX_AGE - MINUTE)
    fresh = msg(-MINUTE)
    [chunk] = build_chunks(run(3), already_extracted=[stale, fresh])
    assert ids(chunk.context) == [fresh.id]


def test_context_is_capped_at_the_overlap_size() -> None:
    history = run(40, start=-40 * MINUTE)
    [chunk] = build_chunks(run(3), already_extracted=history)
    assert ids(chunk.context) == ids(history[-OVERLAP_MESSAGES:])


def test_the_next_chunk_takes_its_context_from_the_previous_chunk_tail() -> None:
    # OVERLAP_MAX_AGE is 6h against a 4h QUIET_GAP precisely so this still has context.
    early = run(MIN_FOR_GAP_BREAK)
    late = run(5, start=after(early, QUIET_GAP))
    first, second = build_chunks(early + late)
    assert first.context == ()
    assert ids(second.context) == ids(early[-OVERLAP_MESSAGES:])


def test_an_overnight_gap_leaves_the_next_chunk_without_context() -> None:
    # Correctly so: nothing said before bed disambiguates a pronoun in the morning.
    evening = run(MIN_FOR_GAP_BREAK)
    morning = run(5, start=after(evening, timedelta(hours=10)))
    _, second = build_chunks(evening + morning)
    assert second.context == ()


def test_handles_map_to_message_ids() -> None:
    history = run(2, start=-30 * MINUTE)
    primary = run(3)
    [chunk] = build_chunks(primary, already_extracted=history)
    assert chunk.handles == {
        "c1": history[0].id,
        "c2": history[1].id,
        "m1": primary[0].id,
        "m2": primary[1].id,
        "m3": primary[2].id,
    }


def test_handles_are_assigned_per_chunk() -> None:
    first, second = build_chunks(run(HARD_MAX + 5))
    assert first.handles["m1"] == first.primary[0].id
    assert second.handles["m1"] == second.primary[0].id
    assert first.handles["m1"] != second.handles["m1"]


def test_a_transcript_line_carries_handle_weekday_household_time_and_sender() -> None:
    # The weekday is not decoration: models resolve "last Tuesday" reliably when it is on
    # the line and unreliably when they must derive it from an ISO date. 20:04 UTC is
    # 21:04 in London in July — the family's clock, never the server's.
    sarah = msg(
        sent_at=datetime(2026, 7, 14, 20, 4, tzinfo=UTC),
        text="she had a bad night again, up 4 times",
    )
    tom = msg(
        sent_at=datetime(2026, 7, 14, 17, 2, tzinfo=UTC),
        sender_display_name="Tom",
        text="is she still off her food?",
    )
    [chunk] = build_chunks([sarah], already_extracted=[tom])
    assert render_transcript(chunk, LONDON) == (
        "[c1] context_only Tue 2026-07-14 18:02 — Tom: is she still off her food?\n"
        "[m1] Tue 2026-07-14 21:04 — Sarah: she had a bad night again, up 4 times"
    )


def test_media_renders_as_a_placeholder_and_never_as_invented_content() -> None:
    photo = msg(message_type="image", text=None)
    voice = msg(MINUTE, message_type="audio", text=None)
    document = msg(
        2 * MINUTE, message_type="document", text=None, media_filename="discharge-letter.pdf"
    )
    [chunk] = build_chunks([photo, voice, document])
    assert render_transcript(chunk, LONDON).splitlines() == [
        "[m1] Tue 2026-07-14 09:00 — Sarah: [photo]",
        "[m2] Tue 2026-07-14 09:01 — Sarah: [voice note]",
        "[m3] Tue 2026-07-14 09:02 — Sarah: [document: discharge-letter.pdf]",
    ]


def test_an_unknown_media_type_still_announces_itself() -> None:
    [chunk] = build_chunks([msg(message_type="sticker", text=None)])
    assert render_transcript(chunk, LONDON).endswith("Sarah: [sticker]")


def test_a_media_caption_is_kept_beside_the_placeholder() -> None:
    [chunk] = build_chunks([msg(message_type="image", text="look at her ankle")])
    assert render_transcript(chunk, LONDON).endswith("Sarah: [photo] look at her ankle")


def test_a_multiline_message_renders_as_one_line() -> None:
    # A raw newline would let the model read half a message as a separate, unhandled one.
    [chunk] = build_chunks([msg(text="bloods taken\n\nreview in 2 weeks")])
    assert render_transcript(chunk, LONDON).endswith("Sarah: bloods taken review in 2 weeks")


def test_an_unattributed_message_still_renders() -> None:
    [chunk] = build_chunks([msg(sender_display_name=None, text="hi")])
    assert render_transcript(chunk, LONDON).endswith("— Unknown: hi")


def test_estimated_chunk_count_rounds_up() -> None:
    assert estimated_chunk_count(0) == 0
    assert estimated_chunk_count(1) == 1
    assert estimated_chunk_count(TARGET) == 1
    assert estimated_chunk_count(TARGET + 1) == 2
