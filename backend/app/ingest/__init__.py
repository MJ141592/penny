"""Inbound message ingestion. The contract is frozen in M0; the seam lands in M6."""

from app.ingest.contract import (
    GROUP_JID_SUFFIX,
    InboundMessage,
    IngestResult,
    MessageSink,
    Provider,
    UnknownGroupError,
    export_group_external_id,
)

__all__ = [
    "GROUP_JID_SUFFIX",
    "InboundMessage",
    "IngestResult",
    "MessageSink",
    "Provider",
    "UnknownGroupError",
    "export_group_external_id",
]
