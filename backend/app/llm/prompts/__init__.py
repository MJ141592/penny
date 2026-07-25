"""Prompt text, loaded once, with a version derived from its own bytes.

The version goes into `llm_runs.prompt_version`, so any row in the audit trail can be traced
to the exact prompt that produced it. Deriving it from a hash rather than a hand-bumped
constant means it cannot drift from the file — editing the prompt IS bumping the version.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_HERE = Path(__file__).parent


def _load(name: str) -> tuple[str, str]:
    text = (_HERE / name).read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


EXTRACT_PROMPT, EXTRACT_PROMPT_VERSION = _load("extract.md")
MERGE_PROMPT, MERGE_PROMPT_VERSION = _load("merge.md")
