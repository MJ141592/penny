"""Build an honest, citation-preserving care summary from stored timeline events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.models import Event

SECTION_NAMES = {
    "symptom": "Symptoms and wellbeing",
    "appointment": "Appointments",
    "medication": "Medication",
    "note": "Care and logistics",
}


def build_report_content(events: Iterable[Event]) -> dict[str, Any]:
    """Summarise what is in the record without inferring diagnoses or causation."""
    rows = list(events)
    grouped: dict[str, list[tuple[str, Event]]] = defaultdict(list)
    for index, event in enumerate(rows, start=1):
        grouped[event.kind].append((f"E{index}", event))

    sections: list[dict[str, Any]] = []
    for kind in ("symptom", "appointment", "medication", "note"):
        items = grouped.get(kind, [])
        if not items:
            continue
        sentences: list[str] = []
        citations: list[dict[str, Any]] = []
        for handle, event in items:
            body = f": {event.body.strip()}" if event.body else ""
            sentences.append(f"{event.title}{body} [{handle}]")
            citations.append(
                {
                    "handle": handle,
                    "event_id": str(event.id),
                    "kind": event.kind,
                    "occurred_at": _utc(event.occurred_at),
                    "title": event.title,
                }
            )
        sections.append(
            {
                "heading": SECTION_NAMES[kind],
                "body_markdown": " ".join(sentences),
                "citations": citations,
            }
        )

    counts = {kind: len(grouped.get(kind, [])) for kind in SECTION_NAMES}
    if rows:
        summary = (
            f"This summary covers {len(rows)} recorded care events: "
            f"{counts['symptom']} symptom updates, {counts['appointment']} appointments, "
            f"{counts['medication']} medication updates and {counts['note']} care notes."
        )
    else:
        summary = "No care events were recorded during this period."

    watch_items = list(dict.fromkeys(event.title for _, event in grouped.get("symptom", [])))[:5]
    data_gaps = [
        sentence
        for kind, sentence in (
            ("appointment", "No appointments were recorded during this period."),
            ("medication", "No medication updates were recorded during this period."),
        )
        if not grouped.get(kind)
    ]
    return {
        "summary": summary,
        "sections": sections,
        "watch_items": watch_items,
        "data_gaps": data_gaps,
    }


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
