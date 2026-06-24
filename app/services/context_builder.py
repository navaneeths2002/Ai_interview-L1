import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_invite_token
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.ats_score import AtsScore
from app.models.interview import Interview, InterviewContext
from app.schemas.interview import TriggerInterviewRequest
from app.realtime.room_manager import create_interview_room
from app.services.resume_extractor import extract_resume_data
from app.services.ats_extractor import extract_ats_data
from app.services.strategy_builder import build_interview_strategy
from app.services.email_service import send_interview_invite
from app.services import cost_tracker

logger = logging.getLogger(__name__)


async def build_interview_context(
    request: TriggerInterviewRequest,
    tenant_id: str,
    db: AsyncSession,
) -> dict:
    """
    Main orchestrator for Phase 3.
    Extracts all data, builds strategy, saves everything to DB.

    Candidate and Job records are UPSERTED — if a record with the same
    (tenant_id, ats_*_id) already exists it is reused and updated, so
    re-triggering an interview for the same candidate/job never crashes.

    Returns interview_id and join_url.
    """

    # Step 1 — Extract resume data from ATS parser response
    resume = extract_resume_data(request.parsed_resume)

    # ── Step 2 — Upsert Candidate ──────────────────────────────────────────────
    # If this candidate was already triggered (same tenant + ats_candidate_id),
    # reuse the existing row and update the profile with the latest resume data.
    existing_candidate = (await db.execute(
        select(Candidate).where(
            and_(
                Candidate.tenant_id       == tenant_id,
                Candidate.ats_candidate_id == request.ats_candidate_id,
            )
        )
    )).scalar_one_or_none()

    new_profile = {
        "total_experience_years": resume["total_experience_years"],
        "current_company":        resume["current_company"],
        "current_role":           resume["current_role"],
        "skills":                 resume["skills"]        or [],
        "certifications":         resume["certifications"] or [],
        "languages":              resume["languages"]      or [],
        "education":              resume["education"],
        "experience":             resume["experience"],
        "projects":               resume["projects"],
        "resume_s3_key":          None,
        "parsed_s3_key":          None,
    }

    if existing_candidate:
        # Update with latest resume data
        existing_candidate.first_name = resume["first_name"] or request.candidate_name.split()[0]
        existing_candidate.last_name  = resume["last_name"]  or (request.candidate_name.split()[-1] if len(request.candidate_name.split()) > 1 else "")
        existing_candidate.email      = resume["email"] or request.candidate_email
        existing_candidate.phone      = resume["phone"] or request.candidate_phone
        existing_candidate.profile    = new_profile
        candidate = existing_candidate
        logger.info(f"[context] reusing existing candidate {candidate.id} ({request.ats_candidate_id})")
    else:
        candidate = Candidate(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            ats_candidate_id=request.ats_candidate_id,
            first_name=resume["first_name"] or request.candidate_name.split()[0],
            last_name=resume["last_name"] or (request.candidate_name.split()[-1] if len(request.candidate_name.split()) > 1 else ""),
            email=resume["email"] or request.candidate_email,
            phone=resume["phone"] or request.candidate_phone,
            profile=new_profile,
        )
        db.add(candidate)
        logger.info(f"[context] created new candidate {candidate.id} ({request.ats_candidate_id})")

    # ── Step 4 — Upsert Job ────────────────────────────────────────────────────
    # If the same job was already used, reuse the row and update its details.
    job_input = request.job

    existing_job = (await db.execute(
        select(Job).where(
            and_(
                Job.tenant_id  == tenant_id,
                Job.ats_job_id == job_input.ats_job_id,
            )
        )
    )).scalar_one_or_none()

    if existing_job:
        # Update with latest JD data
        existing_job.position_title        = job_input.position_title
        existing_job.department            = job_input.department
        existing_job.location              = job_input.location
        existing_job.position_type         = job_input.position_type
        existing_job.min_experience_years  = job_input.min_experience_years
        existing_job.critical_skills       = job_input.critical_skills
        existing_job.optional_skills       = job_input.optional_skills
        existing_job.soft_skills           = job_input.soft_skills
        existing_job.salary_min            = job_input.salary_min
        existing_job.salary_max            = job_input.salary_max
        existing_job.jd_text               = " ".join(job_input.jd_text)
        job = existing_job
        logger.info(f"[context] reusing existing job {job.id} ({job_input.ats_job_id})")
    else:
        job = Job(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            ats_job_id=job_input.ats_job_id,
            position_title=job_input.position_title,
            department=job_input.department,
            location=job_input.location,
            position_type=job_input.position_type,
            min_experience_years=job_input.min_experience_years,
            critical_skills=job_input.critical_skills,
            optional_skills=job_input.optional_skills,
            soft_skills=job_input.soft_skills,
            salary_min=job_input.salary_min,
            salary_max=job_input.salary_max,
            jd_text=" ".join(job_input.jd_text),
        )
        db.add(job)
        logger.info(f"[context] created new job {job.id} ({job_input.ats_job_id})")

    # Step 5 — Extract ATS score data + calculate missing skills
    ats_data = extract_ats_data(
        ats_score_data=request.ats_score_data,
        critical_skills=job_input.critical_skills,
        candidate_skills=resume["skills"],
        resume_filename=request.resume_filename,
    )

    # ── Step 6 — Upsert ATS score ──────────────────────────────────────────────
    existing_ats = (await db.execute(
        select(AtsScore).where(
            and_(
                AtsScore.tenant_id    == tenant_id,
                AtsScore.candidate_id == candidate.id,
                AtsScore.job_id       == job.id,
            )
        )
    )).scalar_one_or_none()

    if existing_ats:
        existing_ats.total_score           = ats_data["total_score"]
        existing_ats.critical_skills_score = ats_data["critical_skills_score"]
        existing_ats.experience_score      = ats_data["experience_score"]
        existing_ats.education_score       = ats_data["education_score"]
        existing_ats.soft_skills_score     = ats_data["soft_skills_score"]
        existing_ats.missing_skills        = ats_data["missing_skills"]
        existing_ats.strong_areas          = ats_data["strong_areas"]
        existing_ats.risk_flags            = ats_data["risk_flags"]
        existing_ats.score_breakdown       = ats_data["score_breakdown"]
        ats_score = existing_ats
    else:
        ats_score = AtsScore(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            candidate_id=candidate.id,
            job_id=job.id,
            total_score=ats_data["total_score"],
            critical_skills_score=ats_data["critical_skills_score"],
            experience_score=ats_data["experience_score"],
            education_score=ats_data["education_score"],
            soft_skills_score=ats_data["soft_skills_score"],
            missing_skills=ats_data["missing_skills"],
            strong_areas=ats_data["strong_areas"],
            risk_flags=ats_data["risk_flags"],
            score_breakdown=ats_data["score_breakdown"],
        )
        db.add(ats_score)

    # Step 7 — Build interview strategy using Claude
    strategy = build_interview_strategy(
        candidate_name=f"{candidate.first_name} {candidate.last_name}",
        current_role=resume["current_role"],
        current_company=resume["current_company"],
        total_experience_years=resume["total_experience_years"],
        skills=resume["skills"],
        experience=resume["experience"],
        projects=resume["projects"],
        missing_skills=ats_data["missing_skills"],
        strong_areas=ats_data["strong_areas"],
        risk_flags=ats_data["risk_flags"],
        job_title=job_input.position_title,
        critical_skills=job_input.critical_skills,
        ats_total_score=ats_data["total_score"],
    )

    _weights_doc = strategy.get("evaluation_weights_doc") or {}
    logger.info(
        f"[context] role-tuned weights ({_weights_doc.get('source')}, "
        f"{_weights_doc.get('role_category')}): {_weights_doc.get('weights')}"
    )

    # Step 8 — Create LiveKit room + interview record
    interview_id = str(uuid.uuid4())
    await create_interview_room(interview_id)

    # Generate a signed invite token — embedded in the join link so only the
    # intended candidate (with this link) can enter the room.
    invite_token = create_invite_token(
        interview_id=interview_id,
        candidate_email=candidate.email,
    )
    join_url = (
        f"{settings.app_base_url}/interview/{interview_id}"
        f"?token={invite_token}"
    )

    interview = Interview(
        id=interview_id,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        job_id=job.id,
        status="scheduled",
        mode="browser",
        join_url=join_url,
        join_expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.invite_token_expire_hours),
        scheduled_at=datetime.now(timezone.utc),
    )
    db.add(interview)

    # Step 9 — Save interview context
    context = InterviewContext(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        interview_id=interview_id,
        skills_to_validate=strategy.get("skills_to_validate", []),
        gaps_to_probe=strategy.get("gaps_to_probe", ats_data["missing_skills"]),
        projects_to_ask=strategy.get("projects_to_ask", []),
        experience_to_verify={"claims": strategy.get("experience_to_verify", [])},
        interview_strategy=strategy.get("strategy_summary", ""),
        question_flow={},
        # Role-tuned evaluation weights (always present + valid — built in strategy_builder)
        evaluation_weights=strategy.get("evaluation_weights_doc"),
    )
    db.add(context)

    await db.flush()

    # Record the strategy Claude call's token usage for per-interview cost tracking.
    _tu = strategy.get("token_usage") or {}
    asyncio.create_task(cost_tracker.patch_usage(interview_id, tenant_id, {
        "strategy_in":  _tu.get("strategy_in", 0),
        "strategy_out": _tu.get("strategy_out", 0),
    }))

    # Send invite email to candidate (fire-and-forget — never blocks the API response)
    asyncio.create_task(send_interview_invite(
        candidate_name=f"{candidate.first_name} {candidate.last_name}",
        candidate_email=candidate.email,
        job_title=job_input.position_title,
        join_url=join_url,
        expire_hours=settings.invite_token_expire_hours,
    ))

    return {
        "interview_id": interview_id,
        "candidate_id": candidate.id,
        "join_url": join_url,
        "candidate_name": f"{candidate.first_name} {candidate.last_name}",
        "missing_skills": ats_data["missing_skills"],
        "strategy_summary": strategy.get("strategy_summary", ""),
        "evaluation_weights": _weights_doc,
    }
