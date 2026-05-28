"""
Phase 8 -- Recovery Admin Routes
==================================
GET  /api/v1/admin/recovery/status  -- counts of stuck/missing interviews
POST /api/v1/admin/recovery/run     -- manually trigger full recovery pass

Both endpoints require X-Tenant-ID header (enforced by TenantMiddleware).
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header
from sqlalchemy import select, func, and_, not_, exists

from app.db.session import AsyncSessionLocal
from app.models.interview import Interview, InterviewScore, InterviewTranscript
from app.models.report import InterviewReport
from app.services.recovery import run_all_recovery

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/v1/admin/recovery/status
# ---------------------------------------------------------------------------

@router.get("/admin/recovery/status")
async def recovery_status(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Returns counts of interviews in various broken states.
    Useful for monitoring dashboards and alerting.
    """
    now = datetime.now(timezone.utc)
    stuck_cutoff     = now - timedelta(hours=2)
    abandoned_cutoff = now - timedelta(hours=24)

    async with AsyncSessionLocal() as db:
        # 1. Stuck in_progress > 2 h
        stuck_count = (await db.execute(
            select(func.count()).select_from(Interview).where(
                and_(
                    Interview.status == "in_progress",
                    Interview.started_at < stuck_cutoff,
                )
            )
        )).scalar_one()

        # 2. Completed with no evaluation score
        missing_eval_count = (await db.execute(
            select(func.count()).select_from(Interview).where(
                and_(
                    Interview.status == "completed",
                    not_(
                        exists(
                            select(InterviewScore.interview_id).where(
                                InterviewScore.interview_id == Interview.id
                            )
                        )
                    ),
                )
            )
        )).scalar_one()

        # 3. Completed + has score but missing report
        missing_report_count = (await db.execute(
            select(func.count()).select_from(Interview).where(
                and_(
                    Interview.status == "completed",
                    exists(
                        select(InterviewScore.interview_id).where(
                            InterviewScore.interview_id == Interview.id
                        )
                    ),
                    not_(
                        exists(
                            select(InterviewReport.interview_id).where(
                                InterviewReport.interview_id == Interview.id
                            )
                        )
                    ),
                )
            )
        )).scalar_one()

        # 4. Scheduled > 24 h with no transcript (abandoned)
        abandoned_count = (await db.execute(
            select(func.count()).select_from(Interview).where(
                and_(
                    Interview.status == "scheduled",
                    Interview.created_at < abandoned_cutoff,
                    not_(
                        exists(
                            select(InterviewTranscript.interview_id).where(
                                InterviewTranscript.interview_id == Interview.id
                            )
                        )
                    ),
                )
            )
        )).scalar_one()

    total_broken = stuck_count + missing_eval_count + missing_report_count + abandoned_count

    return {
        "status": "ok" if total_broken == 0 else "degraded",
        "checked_at": now.isoformat(),
        "stuck_in_progress":       int(stuck_count),
        "missing_evaluations":     int(missing_eval_count),
        "missing_reports":         int(missing_report_count),
        "abandoned_scheduled":     int(abandoned_count),
        "total_requiring_recovery": int(total_broken),
    }


# ---------------------------------------------------------------------------
# POST /api/v1/admin/recovery/run
# ---------------------------------------------------------------------------

@router.post("/admin/recovery/run")
async def trigger_recovery(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Manually trigger a full recovery pass.
    Runs all four recovery functions synchronously and returns a summary.
    """
    logger.info(f"[recovery] manual recovery triggered by tenant {x_tenant_id}")
    summary = await run_all_recovery()
    return {
        "message":  "Recovery pass completed",
        "summary":  summary,
        "ran_at":   datetime.now(timezone.utc).isoformat(),
    }
