"""
ATS Integration endpoints (PUSH-import model)
=============================================
Two endpoints, exempt from TenantMiddleware (tenant is in the body; no API key —
protect at the network level for now / testing).

    1. POST /api/v1/integration/import          ← ATS calls this
       The ATS exports its data as JSON and pushes it here. We store each record
       verbatim in `ats_imports` (upsert on tenant+candidate+job → re-import refreshes).

    2. POST /api/v1/integration/build-payload   ← WE call this
       Given {candidate_id, job_id, tenant_id}, reads the stored record and
       RETURNS the exact JSON body to feed the existing /interviews/trigger.
       (We trigger separately, ourselves, with this output.)

Flow:  ATS ──import──► our db ──build-payload──► trigger JSON ──/trigger──► interview
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.interview import AtsImport
from app.schemas.interview import TriggerInterviewRequest
from app.services import ats_connector

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Import — ATS pushes its exported data here
# ══════════════════════════════════════════════════════════════════════════════

class ImportRecord(BaseModel):
    """One candidate+job the ATS exports. Raw payloads stored verbatim."""
    candidate_id: str
    job_id: str
    candidate_name: str = ""
    candidate_email: str = ""
    candidate_phone: str = ""
    resume_filename: str = ""
    parsed_resume: dict[str, Any] = {}   # raw /parse output
    ats_score: dict[str, Any] = {}       # raw /ats-score output
    jd: dict[str, Any] = {}              # raw JD output


class ImportRequest(BaseModel):
    tenant_id: str
    records: list[ImportRecord]


class ImportResponse(BaseModel):
    imported: int
    tenant_id: str
    message: str


@router.post("/integration/import", response_model=ImportResponse)
async def import_ats_data(
    body: ImportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Store (upsert) every pushed record into ats_imports. Re-import = refresh."""
    now = datetime.now(timezone.utc)
    count = 0
    for rec in body.records:
        existing = (await db.execute(
            select(AtsImport).where(
                AtsImport.tenant_id == body.tenant_id,
                AtsImport.ats_candidate_id == rec.candidate_id,
                AtsImport.ats_job_id == rec.job_id,
            )
        )).scalar_one_or_none()

        row = existing or AtsImport(
            tenant_id=body.tenant_id,
            ats_candidate_id=rec.candidate_id,
            ats_job_id=rec.job_id,
        )
        if existing is None:
            db.add(row)

        row.candidate_name  = rec.candidate_name
        row.candidate_email = rec.candidate_email
        row.candidate_phone = rec.candidate_phone
        row.resume_filename = rec.resume_filename
        row.parsed_resume   = rec.parsed_resume
        row.ats_score       = rec.ats_score
        row.jd              = rec.jd
        row.imported_at     = now
        count += 1

    await db.commit()
    return ImportResponse(
        imported=count,
        tenant_id=body.tenant_id,
        message=f"Imported/refreshed {count} record(s).",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Build payload — turn a stored record into the trigger JSON
# ══════════════════════════════════════════════════════════════════════════════

class BuildPayloadRequest(BaseModel):
    candidate_id: str
    job_id: str
    tenant_id: str


@router.post("/integration/build-payload", response_model=TriggerInterviewRequest)
async def build_payload(
    body: BuildPayloadRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Return the exact JSON body for POST /api/v1/interviews/trigger, built from the
    stored import. Feed this output to the trigger endpoint (with the tenant as
    the X-Tenant-ID header) to start the interview.
    """
    row = (await db.execute(
        select(AtsImport).where(
            AtsImport.tenant_id == body.tenant_id,
            AtsImport.ats_candidate_id == body.candidate_id,
            AtsImport.ats_job_id == body.job_id,
        )
    )).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No imported record for candidate={body.candidate_id}, "
                f"job={body.job_id}, tenant={body.tenant_id}. "
                "Run POST /integration/import first."
            ),
        )

    return ats_connector.build_trigger_request(row)
