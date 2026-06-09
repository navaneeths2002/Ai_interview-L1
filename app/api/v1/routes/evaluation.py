"""
Phase 6 — Evaluation API Routes

GET  /api/v1/interviews/{id}/evaluation   → fetch evaluation results (recruiter-facing)
POST /api/v1/interviews/{id}/evaluate     → manually trigger evaluation (admin/testing)
"""

import asyncio
from fastapi import APIRouter, HTTPException, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.rate_limiter import limiter, LIMIT_EVALUATE
from app.db.session import get_db
from app.models.interview import Interview, InterviewScore, InterviewExtractedData
from app.services.evaluation_engine import run_evaluation

router = APIRouter()


# ── GET /api/v1/interviews/{id}/evaluation ─────────────────────────────────────

@router.get("/interviews/{interview_id}/evaluation")
async def get_evaluation(
    interview_id: str,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Returns the evaluation scores and extracted data for a completed interview.
    Requires X-Tenant-ID header.
    """
    # Verify interview belongs to tenant
    interview = (await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.tenant_id == x_tenant_id,
        )
    )).scalar_one_or_none()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Interview is not completed (current status: {interview.status})"
        )

    # Fetch scores
    score = (await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )).scalar_one_or_none()

    if not score:
        raise HTTPException(
            status_code=404,
            detail="Evaluation not yet available. It may still be processing."
        )

    # Fetch extracted data
    extracted = (await db.execute(
        select(InterviewExtractedData).where(
            InterviewExtractedData.interview_id == interview_id
        )
    )).scalar_one_or_none()

    return {
        "interview_id":   interview_id,
        "status":         interview.status,
        "duration_seconds": interview.duration_seconds,

        # ── Scores ────────────────────────────────────────────────────────────
        "scores": {
            "communication":        score.communication_score,
            "confidence":           score.confidence_score,
            "jd_fit":               score.jd_fit_score,
            "behavioral":           score.behavioral_score,
            "overall":              score.overall_score,
            "salary_fit":           score.salary_fit,
            "experience_validated": score.experience_validated,
        },

        # ── Recommendation ─────────────────────────────────────────────────────
        "recommendation": {
            "ai":              score.recommendation,
            "recruiter":       score.recruiter_override,
            "override_reason": score.override_reason,
        },

        # ── AI reasoning (summary + strengths/weaknesses/flags) ────────────────
        "ai_reasoning": score.ai_reasoning,

        # ── Extracted interview data ───────────────────────────────────────────
        # Data lives in extracted JSONB column (not individual typed columns)
        "extracted_data": extracted.extracted if extracted else None,
    }


# ── POST /api/v1/interviews/{id}/evaluate ─────────────────────────────────────

@router.post("/interviews/{interview_id}/evaluate", status_code=202)
@limiter.limit(LIMIT_EVALUATE)
async def trigger_evaluation(
    request: Request,
    interview_id: str,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Manually triggers the evaluation engine for a completed interview.
    Useful for re-running evaluation or for testing.
    Returns 202 Accepted — evaluation runs in the background.
    """
    interview = (await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.tenant_id == x_tenant_id,
        )
    )).scalar_one_or_none()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Interview must be completed before evaluation (status: {interview.status})"
        )

    # Fire and forget
    asyncio.create_task(run_evaluation(interview_id))

    return {
        "message":      "Evaluation started",
        "interview_id": interview_id,
        "status":       "processing",
    }
