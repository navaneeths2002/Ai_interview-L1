"""
Phase 7 — Report API Routes

GET  /api/v1/interviews/{id}/report       → structured JSON report
GET  /api/v1/interviews/{id}/report/html  → browser-renderable HTML (print-to-PDF)
POST /api/v1/interviews/{id}/report       → trigger / re-generate report
"""

import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.rate_limiter import limiter, LIMIT_REPORT
from app.core.security import verify_report_token
from app.db.session import get_db
from app.models.interview import Interview
from app.models.report import InterviewReport
from app.services.report_generator import run_report

router = APIRouter()


# ── GET /api/v1/interviews/{id}/report ────────────────────────────────────────

@router.get("/interviews/{interview_id}/report")
async def get_report(
    interview_id: str,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Returns the full structured JSON report for a completed interview.
    """
    _verify_tenant(await _get_interview(db, interview_id, x_tenant_id))

    report = await _get_report_row(db, interview_id)
    if not report:
        raise HTTPException(
            status_code=404,
            detail="Report not yet available. Trigger generation with POST /report."
        )

    return {
        "interview_id":  interview_id,
        "generated_at":  report.generated_at.isoformat() if report.generated_at else None,
        "candidate_name": report.candidate_name,
        "position_title": report.position_title,
        "overall_score":  report.overall_score,
        "recommendation": report.recommendation,
        "report":         report.report_data,
    }


# ── GET /api/v1/interviews/{id}/report/html ───────────────────────────────────

@router.get("/interviews/{interview_id}/report/html", response_class=HTMLResponse)
async def get_report_html(
    interview_id: str,
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the pre-rendered HTML report.
    Requires a signed report access token passed as ?token= query param.
    The token is embedded in report_url when the report is generated.
    Use Ctrl+P → Save as PDF for a shareable document.
    """
    # Validate signed report token — prevents unauthenticated access to any
    # candidate's report by guessing interview UUIDs.
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Report access token required. Use the link from the recruiter dashboard.",
        )
    try:
        verify_report_token(token, interview_id)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    report = await _get_report_row(db, interview_id)
    if not report or not report.report_html:
        raise HTTPException(
            status_code=404,
            detail="HTML report not yet available. The interview may still be processing."
        )

    return HTMLResponse(content=report.report_html)


# ── POST /api/v1/interviews/{id}/report ──────────────────────────────────────

@router.post("/interviews/{interview_id}/report", status_code=202)
@limiter.limit(LIMIT_REPORT)
async def trigger_report(
    request: Request,
    interview_id: str,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Triggers (or re-triggers) report generation.
    Returns 202 Accepted — generation runs in the background.
    """
    interview = await _get_interview(db, interview_id, x_tenant_id)

    if interview.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Interview must be completed first (status: {interview.status})"
        )

    asyncio.create_task(run_report(interview_id))

    return {
        "message":      "Report generation started",
        "interview_id": interview_id,
        "status":       "processing",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_interview(db: AsyncSession, interview_id: str, tenant_id: str) -> Interview:
    row = (await db.execute(
        select(Interview).where(
            Interview.id        == interview_id,
            Interview.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Interview not found")
    return row


def _verify_tenant(interview: Interview) -> None:
    """Placeholder — tenant already verified in _get_interview."""
    pass


async def _get_report_row(db: AsyncSession, interview_id: str) -> InterviewReport | None:
    return (await db.execute(
        select(InterviewReport).where(InterviewReport.interview_id == interview_id)
    )).scalar_one_or_none()
