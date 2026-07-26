"""Read and generate household-scoped care summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from app.deps import CurrentHousehold, SessionDep
from app.errors import NotFoundError
from app.models import Event, Report
from app.reporting import build_report_content
from app.schemas import (
    ReportCreate,
    ReportOut,
    ReportSummaryOut,
    to_report,
    to_report_summary,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[ReportSummaryOut])
async def list_reports(ctx: CurrentHousehold, session: SessionDep) -> list[ReportSummaryOut]:
    rows = (
        await session.scalars(
            select(Report)
            .where(Report.household_id == ctx.id)
            .order_by(Report.period_end.desc(), Report.created_at.desc())
        )
    ).all()
    return [to_report_summary(row) for row in rows]


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: UUID, ctx: CurrentHousehold, session: SessionDep) -> ReportOut:
    row = await session.scalar(
        select(Report).where(Report.id == report_id, Report.household_id == ctx.id)
    )
    if row is None:
        raise NotFoundError
    return to_report(row)


@router.post("", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def create_report(
    create: ReportCreate,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> ReportOut:
    period_end = datetime.now(UTC)
    period_start = period_end - timedelta(days=create.period_days)
    events = (
        await session.scalars(
            select(Event)
            .where(
                Event.household_id == ctx.id,
                Event.deleted_at.is_(None),
                Event.occurred_at >= period_start,
                Event.occurred_at <= period_end,
            )
            .order_by(Event.occurred_at.desc(), Event.id.desc())
            .limit(200)
        )
    ).all()
    content = build_report_content(events)
    report = Report(
        household_id=ctx.id,
        status="complete",
        title=f"Care summary · last {create.period_days} days",
        summary=content["summary"],
        urgent_flag=False,
        urgent_reason=None,
        sections=content["sections"],
        questions_for_the_doctor=[],
        watch_items=content["watch_items"],
        data_gaps=content["data_gaps"],
        period_start=period_start,
        period_end=period_end,
        generated_at=period_end,
    )
    session.add(report)
    await session.flush()
    return to_report(report)
