"""
ATS mapping helpers
===================
Turns a stored `ats_imports` record — the raw ATS JSON the ATS PUSHED to us via
POST /api/v1/integration/import — into a TriggerInterviewRequest.

There is NO connection to the ATS here (no DB, no HTTP). The data already lives
in our own `ats_imports` table; this module only maps it into the trigger shape.

╔══════════════════════════════════════════════════════════════════════════════╗
║  TODO(ATS): the JD JSON key names below are a best guess. Once the real ATS   ║
║  export shape is known, adjust the JD_*_KEY values to match.                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas.interview import JobInput, TriggerInterviewRequest

logger = logging.getLogger(__name__)


# ── keys INSIDE the JD JSON (TODO(ATS): match your JD generator's field names) ──
JD_TITLE_KEY      = "position_title"
JD_DEPT_KEY       = "department"
JD_LOCATION_KEY   = "location"
JD_TYPE_KEY       = "position_type"
JD_MIN_EXP_KEY    = "min_experience_years"
JD_CRITICAL_KEY   = "critical_skills"
JD_OPTIONAL_KEY   = "optional_skills"
JD_SOFT_KEY       = "soft_skills"
JD_SALARY_MIN_KEY = "salary_min"
JD_SALARY_MAX_KEY = "salary_max"
JD_TEXT_KEY       = "jd_text"


# ── helpers ───────────────────────────────────────────────────────────────────

def _as_json(value: Any) -> Any:
    """A JSON column may come back as a str — decode to dict/list if so."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _as_list(value: Any) -> list:
    """Coerce a JD field into a list of strings (handles str, list, or None)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _derive_filename(parsed_resume: Any, explicit: str | None) -> str:
    """
    resume_filename is the join key the agent uses to pick the right entry.
    Prefer an explicit value; otherwise fall back to the single top-level key of
    the parse JSON (the /parse output is keyed by filename).
    """
    if explicit:
        return str(explicit)
    if isinstance(parsed_resume, dict) and len(parsed_resume) == 1:
        return next(iter(parsed_resume.keys()))
    return ""


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_job_input(job_json: Any, ats_job_id: str) -> JobInput:
    """Map the JD JSON → JobInput. TODO(ATS): confirm the JD_*_KEY names above."""
    j = _as_json(job_json) or {}
    if not isinstance(j, dict):
        j = {}
    return JobInput(
        ats_job_id=str(ats_job_id),
        position_title=str(j.get(JD_TITLE_KEY) or ""),
        department=str(j.get(JD_DEPT_KEY) or ""),
        location=str(j.get(JD_LOCATION_KEY) or ""),
        position_type=str(j.get(JD_TYPE_KEY) or "full_time"),
        min_experience_years=_to_int(j.get(JD_MIN_EXP_KEY)),
        critical_skills=_as_list(j.get(JD_CRITICAL_KEY)),
        optional_skills=_as_list(j.get(JD_OPTIONAL_KEY)),
        soft_skills=_as_list(j.get(JD_SOFT_KEY)),
        salary_min=_to_int(j.get(JD_SALARY_MIN_KEY)),
        salary_max=_to_int(j.get(JD_SALARY_MAX_KEY)),
        jd_text=_as_list(j.get(JD_TEXT_KEY)),
    )


# ── public entry point ────────────────────────────────────────────────────────

def build_trigger_request(record) -> TriggerInterviewRequest:
    """
    Build a TriggerInterviewRequest from a stored AtsImport row (or any object
    exposing the same attributes). Pure mapping — no I/O.
    """
    parsed_resume = _as_json(record.parsed_resume) or {}
    ats_score_data = _as_json(record.ats_score) or {}
    resume_filename = _derive_filename(parsed_resume, record.resume_filename)

    payload = TriggerInterviewRequest(
        ats_candidate_id=str(record.ats_candidate_id),
        candidate_name=str(record.candidate_name or "Candidate"),
        candidate_email=str(record.candidate_email or "").strip(),
        candidate_phone=str(record.candidate_phone or ""),
        resume_filename=resume_filename,
        parsed_resume=parsed_resume if isinstance(parsed_resume, dict) else {},
        ats_score_data=ats_score_data if isinstance(ats_score_data, dict) else {},
        job=_build_job_input(record.jd, record.ats_job_id),
    )
    logger.info(
        f"[ats-connector] built trigger payload from import "
        f"candidate={record.ats_candidate_id} job={record.ats_job_id}"
    )
    return payload
