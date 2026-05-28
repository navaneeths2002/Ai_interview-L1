import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.ats_score import AtsScore
from app.models.interview import Interview, InterviewContext
from app.schemas.interview import TriggerInterviewRequest
from app.realtime.room_manager import create_interview_room
from app.services.resume_extractor import extract_resume_data
from app.services.ats_extractor import extract_ats_data
from app.services.strategy_builder import build_interview_strategy


async def build_interview_context(
    request: TriggerInterviewRequest,
    tenant_id: str,
    db: AsyncSession,
) -> dict:
    """
    Main orchestrator for Phase 3.
    Extracts all data, builds strategy, saves everything to DB.
    Returns interview_id and join_url.
    """

    # Step 1 — Extract resume data from ATS parser response
    resume = extract_resume_data(request.parsed_resume)

    # Step 2 — Save candidate (with embedded profile JSONB) to DB
    candidate = Candidate(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        ats_candidate_id=request.ats_candidate_id,
        first_name=resume["first_name"] or request.candidate_name.split()[0],
        last_name=resume["last_name"] or (request.candidate_name.split()[-1] if len(request.candidate_name.split()) > 1 else ""),
        email=resume["email"] or request.candidate_email,
        phone=resume["phone"] or request.candidate_phone,
        # All resume-parsed profile data lives in a single JSONB document
        profile={
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
        },
    )
    db.add(candidate)

    # Step 4 — Save job to DB
    job_input = request.job
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

    # Step 5 — Extract ATS score data + calculate missing skills
    ats_data = extract_ats_data(
        ats_score_data=request.ats_score_data,
        critical_skills=job_input.critical_skills,
        candidate_skills=resume["skills"],
        resume_filename=request.resume_filename,
    )

    # Step 6 — Save ATS score to DB
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

    # Step 8 — Create LiveKit room + interview record
    interview_id = str(uuid.uuid4())
    await create_interview_room(interview_id)
    join_url = f"http://localhost:8000/interview/{interview_id}"

    interview = Interview(
        id=interview_id,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        job_id=job.id,
        status="scheduled",
        mode="browser",
        join_url=join_url,
        join_expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
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
    )
    db.add(context)

    await db.flush()

    return {
        "interview_id": interview_id,
        "candidate_id": candidate.id,
        "join_url": join_url,
        "candidate_name": f"{candidate.first_name} {candidate.last_name}",
        "missing_skills": ats_data["missing_skills"],
        "strategy_summary": strategy.get("strategy_summary", ""),
    }
