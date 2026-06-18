from pydantic import BaseModel
from typing import Any


class JobInput(BaseModel):
    """Job details — manually provided for now, later fetched from ATS."""
    ats_job_id: str
    position_title: str
    department: str = ""
    location: str = ""
    position_type: str = "full_time"
    min_experience_years: int = 0
    critical_skills: list[str]
    optional_skills: list[str] = []
    soft_skills: list[str] = []
    salary_min: int = 0
    salary_max: int = 0
    jd_text: list[str] = []   # array of strings from your JD generator


class TriggerInterviewRequest(BaseModel):
    """
    Request body for POST /api/v1/interviews/trigger.

    For now: paste the ATS responses directly here.
    Later: L1 will fetch these automatically from ATS REST endpoints.
    """
    # Basic candidate info
    ats_candidate_id: str
    candidate_name: str
    candidate_email: str
    candidate_phone: str = ""

    # Filename of the resume — used to match the correct result in ats_score_data
    # e.g. "Rajiv_Chaudhary.pdf"
    resume_filename: str

    # Paste the full resume parser response here
    # e.g. { "Rajiv_Chaudhary.pdf": { "first_name": ..., "skills": [...], ... } }
    parsed_resume: dict[str, Any]

    # Paste the full ATS score response here (can have multiple results)
    # e.g. { "results": [ { "file": "Rajiv_Chaudhary.pdf.json", "result": {...} } ] }
    ats_score_data: dict[str, Any]

    # Job details
    job: JobInput


class TriggerInterviewResponse(BaseModel):
    """Response returned to ATS after trigger."""
    interview_id: str
    candidate_id: str
    status: str
    join_url: str
    message: str
    # Role-tuned evaluation weights chosen by the LLM for this interview.
    # {"role_category", "weights": {jd_fit,communication,behavioral,confidence,ats}, "rationale", "source"}
    evaluation_weights: dict[str, Any] | None = None
