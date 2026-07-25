"""The ingestion contract is frozen in M0 and read by four later tracks. Pin it."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.ingest.contract import (
    GROUP_JID_SUFFIX,
    InboundMessage,
    IngestResult,
    MessageSink,
    UnknownGroupError,
    export_group_external_id,
)

SENT_AT = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)


def make_message(**overrides) -> InboundMessage:
    fields = {
        "provider": "gowa",
        "provider_message_id": "3EB0A1B2C3",
        "sender_wa_jid": "447700900123@s.whatsapp.net",
        "sender_wa_lid": "251566778899@lid",
        "sender_display_name": "Sarah",
        "sent_at": SENT_AT,
        "text": "Took Mum to the GP this morning",
    }
    return InboundMessage(**(fields | overrides))


def test_naive_sent_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_message(sent_at=datetime(2026, 7, 17, 9, 30))


def test_tz_aware_sent_at_is_accepted() -> None:
    assert make_message().sent_at == SENT_AT


def test_non_utc_offsets_are_accepted_and_left_for_the_seam_to_normalise() -> None:
    # The .txt parser builds local wall-clock times in the household's zone; requiring
    # adapters to pre-convert would just move the same conversion bug around.
    local = datetime(2026, 7, 17, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    assert make_message(sent_at=local).sent_at == SENT_AT


def test_inbound_message_is_frozen() -> None:
    message = make_message()
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.text = "edited"  # type: ignore[misc]


def test_inbound_message_defaults_match_the_contract() -> None:
    message = make_message()
    assert message.message_type == "text"
    assert message.payload == {}
    assert message.source_ordinal is None


def test_payload_default_is_not_shared_between_messages() -> None:
    first, second = make_message(), make_message()
    first.payload["id"] = "3EB0A1B2C3"
    assert second.payload == {}


def test_inbound_message_field_order_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(InboundMessage)] == [
        "provider",
        "provider_message_id",
        "sender_wa_jid",
        "sender_wa_lid",
        "sender_display_name",
        "sent_at",
        "text",
        "message_type",
        "payload",
        "source_ordinal",
    ]


def test_ingest_result_field_order_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(IngestResult)] == [
        "received",
        "inserted",
        "duplicates",
        "skipped",
        "new_member_ids",
        "first_sent_at",
        "last_sent_at",
        "household_id",
    ]


def test_unknown_group_error_carries_the_group_id() -> None:
    error = UnknownGroupError("120363000000000000@g.us")
    assert error.group_external_id == "120363000000000000@g.us"
    assert "120363000000000000@g.us" in str(error)


def test_export_group_ids_are_scoped_to_one_household() -> None:
    household_id = uuid4()
    assert export_group_external_id(household_id) == f"export:{household_id}"
    assert not export_group_external_id(household_id).endswith(GROUP_JID_SUFFIX)


async def test_a_fake_sink_satisfies_the_protocol() -> None:
    # Track A and Track F code against MessageSink before the seam exists.
    class RecordingSink:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def ingest_messages(self, session, group_external_id, messages) -> IngestResult:
            self.calls.append((group_external_id, len(messages)))
            return IngestResult(
                received=len(messages),
                inserted=len(messages),
                duplicates=0,
                skipped=0,
                new_member_ids=[],
                first_sent_at=SENT_AT,
                last_sent_at=SENT_AT,
                household_id=uuid4(),
            )

    sink: MessageSink = RecordingSink()
    assert isinstance(sink, MessageSink)
    result = await sink.ingest_messages(None, "120363000000000000@g.us", [make_message()])
    assert result.inserted == 1
