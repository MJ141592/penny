"""Turning messages into events. The pure parts live here; the runner owns the IO."""

from app.extraction.chunker import (
    HARD_MAX,
    MAX_SPAN,
    MIN_FOR_GAP_BREAK,
    OVERLAP_MAX_AGE,
    OVERLAP_MESSAGES,
    QUIET_GAP,
    TARGET,
    Chunk,
    ChunkMessage,
    build_chunks,
    estimated_chunk_count,
    render_transcript,
)
from app.extraction.dedup import (
    ExtractedEventLike,
    MergeDecision,
    compute_dedup_key,
    decide_merge,
    human_dedup_key,
    normalise,
)

__all__ = [
    "HARD_MAX",
    "MAX_SPAN",
    "MIN_FOR_GAP_BREAK",
    "OVERLAP_MAX_AGE",
    "OVERLAP_MESSAGES",
    "QUIET_GAP",
    "TARGET",
    "Chunk",
    "ChunkMessage",
    "ExtractedEventLike",
    "MergeDecision",
    "build_chunks",
    "compute_dedup_key",
    "decide_merge",
    "estimated_chunk_count",
    "human_dedup_key",
    "normalise",
    "render_transcript",
]
