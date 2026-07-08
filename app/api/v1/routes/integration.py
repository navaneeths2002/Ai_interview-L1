"""
ATS Integration endpoint
========================
The single door the ATS module calls (PUSH model) to start an interview for a
specific candidate. This path is exempted from TenantMiddleware; the tenant is
supplied in the request body. (No API-key auth — add network-level protection or
re-introduce a key if this endpoint is ever exposed publicly.)

Flow:
    ATS  ──►  POST /api/v1/integration/interviews
              { candidate_id, job_id, tenant_id }
                  │
              ats_connector.fetch_trigger_payload(candidate_id, job_id)   (reads ATS db)
                  │
              build_interview_context(payload, tenant_id, db)             (our existing flow)
                  │
              { interview_id, join_url, status }
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import ats_connector
from app.services.context_builder import build_interview_context

router = APIRouter()


class IntegrationTriggerRequest(BaseModel):
    """What the ATS sends — just the identifiers; we pull the rest from their db."""
    candidate_id: str
    job_id: str
    tenant_id: str


class IntegrationTriggerResponse(BaseModel):
    interview_id: str
    candidate_id: str
    status: str
    join_url: str
    message: str


@router.post(
    "/integration/interviews",
    response_model=IntegrationTriggerResponse,
)
async def integration_trigger(
    body: IntegrationTriggerRequest,
    db: AsyncSession = Depends(get_db),
):
    # 1. Pull the candidate's resume + score + job from the ATS db → trigger body.
    #    ATSDataError carries a precise status + reason (404 not found, 422 no
    #    email, 502 db read failed, 503 not configured) so the ATS team can debug.
    try:
        payload = await ats_connector.fetch_trigger_payload(
            candidate_id=body.candidate_id,
            job_id=body.job_id,
        )
    except ats_connector.ATSDataError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    # 2. Reuse the existing orchestrator: strategy, room, invite token, email.
    try:
        result = await build_interview_context(
            request=payload,
            tenant_id=body.tenant_id,
            db=db,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return IntegrationTriggerResponse(
        interview_id=result["interview_id"],
        candidate_id=result["candidate_id"],
        status="scheduled",
        join_url=result["join_url"],
        message=f"Interview scheduled for {result['candidate_name']}.",
    )
