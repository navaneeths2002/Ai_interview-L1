from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.interview import TriggerInterviewRequest, TriggerInterviewResponse
from app.services.context_builder import build_interview_context

router = APIRouter()


@router.post("/interviews/trigger", response_model=TriggerInterviewResponse)
async def trigger_interview(
    body: TriggerInterviewRequest,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    tenant_id = x_tenant_id

    try:
        result = await build_interview_context(
            request=body,
            tenant_id=tenant_id,
            db=db,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TriggerInterviewResponse(
        interview_id=result["interview_id"],
        candidate_id=result["candidate_id"],
        status="scheduled",
        join_url=result["join_url"],
        message=f"Interview scheduled for {result['candidate_name']}. "
                f"Missing skills to probe: {', '.join(result['missing_skills']) or 'None'}. "
                f"Strategy: {result['strategy_summary']}",
    )
