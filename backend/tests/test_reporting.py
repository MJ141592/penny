import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db as app_db
from app.config import Settings
from app.db import to_asyncpg_url
from app.main import app
from app.models import Event, Household
from app.reporting import build_report_content
from app.security import hash_password


def event(kind: str, title: str, body: str | None = None) -> Event:
    return Event(
        id=uuid4(),
        household_id=uuid4(),
        kind=kind,
        title=title,
        body=body,
        occurred_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        occurred_at_precision="exact",
        details={},
        source_message_ids=[],
        source_excerpts=[],
        occurrences=[],
        user_edited_fields=[],
        dedup_key=f"human:{uuid4()}",
    )


def test_report_groups_events_and_keeps_resolvable_citations() -> None:
    symptom = event("symptom", "Dizziness", "Reported after breakfast")
    medication = event("medication", "Ramipril changed")

    report = build_report_content([symptom, medication])

    assert "2 recorded care events" in report["summary"]
    assert [section["heading"] for section in report["sections"]] == [
        "Symptoms and wellbeing",
        "Medication",
    ]
    citations = [citation for section in report["sections"] for citation in section["citations"]]
    assert [citation["handle"] for citation in citations] == ["E1", "E2"]
    assert [citation["event_id"] for citation in citations] == [
        str(symptom.id),
        str(medication.id),
    ]
    assert report["watch_items"] == ["Dizziness"]


def test_empty_report_states_what_is_missing_without_inventing_content() -> None:
    report = build_report_content([])

    assert report["summary"] == "No care events were recorded during this period."
    assert report["sections"] == []
    assert report["watch_items"] == []
    assert report["data_gaps"] == [
        "No appointments were recorded during this period.",
        "No medication updates were recorded during this period.",
    ]


def test_new_write_routes_are_registered_before_the_spa() -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths["/api/events"]
    assert "post" in paths["/api/reports"]


def run(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        engine = create_async_engine(to_asyncpg_url(db_url))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.mark.db
def test_manual_update_and_report_round_trip(
    db_url: str,
    settings_override: Callable[..., Settings],
) -> None:
    password = "a-real-family-passphrase"
    household = Household(
        id=uuid4(),
        username=f"reporting-{uuid4().hex[:8]}",
        password_hash=hash_password(password),
        name="The Reporters",
        care_recipient_name="Margaret",
        timezone="Europe/London",
    )

    async def create_household(session: AsyncSession) -> None:
        session.add(household)

    run(db_url, create_household)
    settings_override(
        env="test",
        database_url=db_url,
        test_database_url=db_url,
        session_secret="test-session-secret-value-32-chars-min",
    )
    app_db.get_engine.cache_clear()
    app_db.get_sessionmaker.cache_clear()
    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": household.username, "password": password},
            )
            assert login.status_code == 204

            created = client.post(
                "/api/events",
                json={
                    "kind": "note",
                    "title": "Family update",
                    "body": "Margaret had lunch with Liz.",
                    "occurred_at": "2026-07-25T10:00:00Z",
                    "details": {"category": "logistics"},
                },
            )
            assert created.status_code == 201, created.text
            event_id = created.json()["id"]
            assert created.json()["source_excerpts"] == []

            generated = client.post("/api/reports", json={"period_days": 365})
            assert generated.status_code == 201, generated.text
            report = generated.json()
            assert report["status"] == "complete"
            assert report["sections"][0]["citations"][0]["event_id"] == event_id
            assert client.get(f"/api/reports/{report['id']}").json() == report
    finally:
        app_db.get_engine.cache_clear()
        app_db.get_sessionmaker.cache_clear()
        run(
            db_url,
            lambda session: session.execute(
                sa.delete(Household).where(Household.id == household.id)
            ),
        )
