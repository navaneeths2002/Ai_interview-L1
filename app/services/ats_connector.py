"""
ATS Connector — pull candidate data from the ATS module's Postgres
==================================================================
Given a (candidate_id, job_id) the ATS pushes to our integration endpoint, this
reads the FOUR things needed to trigger an interview straight from the ATS's own
(separate, remote) Postgres and assembles a TriggerInterviewRequest:

    1. candidate contact  (name, email, phone)
    2. parsed_resume JSON  (raw /parse output — stored as JSON)
    3. ats_score   JSON    (raw /ats-score output — stored as JSON)
    4. job / JD

Design:
  • READ-ONLY, SEPARATE engine on ATS_DATABASE_URL — the ATS is a different db.
  • Same NullPool style as the main engine (app/db/session.py) — no pool tuning.
  • Self-contained; never writes to the ATS db.

╔══════════════════════════════════════════════════════════════════════════════╗
║  TODO(ATS): the ATS team must confirm their real TABLE + COLUMN names.        ║
║  Fill in every  "<...>"  placeholder in the ATS SCHEMA MAP block below.       ║
║  Until then the queries are intentionally non-functional (they reference      ║
║  placeholder identifiers) — nothing runs against a real db by accident.       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.schemas.interview import JobInput, TriggerInterviewRequest

logger = logging.getLogger(__name__)


class ATSDataError(Exception):
    """
    Raised when ATS data can't be turned into a trigger payload. Carries an HTTP
    status so the integration endpoint surfaces a CLEAR reason to the ATS team
    during their development (not a generic 500).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ══════════════════════════════════════════════════════════════════════════════
#  ATS SCHEMA MAP  —  TODO(ATS): replace every "<...>" with the real identifier
# ══════════════════════════════════════════════════════════════════════════════
# Candidate / application table (holds contact info + the parsed-resume JSON).
CAND_TABLE          = "<candidates_table>"
CAND_ID_COL         = "<candidate_id>"
CAND_NAME_COL       = "<full_name>"
CAND_EMAIL_COL      = "<email>"          # REQUIRED — no email, no interview
CAND_PHONE_COL      = "<phone>"
CAND_RESUME_JSON    = "<parsed_resume_json>"   # JSON: raw /parse output
CAND_RESUME_FILENM  = "<resume_filename>"      # or "" if not a column (derived from JSON)

# ATS score table (one row per candidate+job).
SCORE_TABLE         = "<scores_table>"
SCORE_CAND_COL      = "<candidate_id>"
SCORE_JOB_COL       = "<job_id>"
SCORE_JSON_COL      = "<ats_score_json>"       # JSON: raw /ats-score output

# Job / JD table.
JOB_TABLE           = "<jobs_table>"
JOB_ID_COL          = "<job_id>"
JOB_JSON_COL        = "<jd_json>"              # JSON: raw /generate-job-description output
# ── keys INSIDE the JD JSON (TODO(ATS): match your JD generator's field names) ──
JD_TITLE_KEY        = "position_title"
JD_DEPT_KEY         = "department"
JD_LOCATION_KEY     = "location"
JD_TYPE_KEY         = "position_type"
JD_MIN_EXP_KEY      = "min_experience_years"
JD_CRITICAL_KEY     = "critical_skills"
JD_OPTIONAL_KEY     = "optional_skills"
JD_SOFT_KEY         = "soft_skills"
JD_SALARY_MIN_KEY   = "salary_min"
JD_SALARY_MAX_KEY   = "salary_max"
JD_TEXT_KEY         = "jd_text"
# ══════════════════════════════════════════════════════════════════════════════


_ats_engine = None
_ats_factory = None


def _get_ats_factory():
    """Lazy read-only engine on the ATS db — NullPool, like app/db/session.py."""
    global _ats_engine, _ats_factory
    if _ats_factory is None:
        url = settings.ats_database_url or ""
        if not url:
            logger.error("[ats-connector] ATS_DATABASE_URL not set — ATS pull disabled")
            return None
        _ats_engine = create_async_engine(url, poolclass=NullPool)
        _ats_factory = async_sessionmaker(
            bind=_ats_engine, class_=AsyncSession, expire_on_commit=False
        )
        logger.info("[ats-connector] read-only ATS engine created")
    return _ats_factory


# ── helpers ───────────────────────────────────────────────────────────────────

def _as_json(value: Any) -> Any:
    """asyncpg may return JSON/JSONB as a str — decode to dict/list if so."""
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
    Prefer an explicit column; otherwise fall back to the single top-level key
    of the parse JSON (the /parse output is keyed by filename).
    """
    if explicit:
        return str(explicit)
    if isinstance(parsed_resume, dict) and len(parsed_resume) == 1:
        return next(iter(parsed_resume.keys()))
    return ""


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
        min_experience_years=int(j.get(JD_MIN_EXP_KEY) or 0),
        critical_skills=_as_list(j.get(JD_CRITICAL_KEY)),
        optional_skills=_as_list(j.get(JD_OPTIONAL_KEY)),
        soft_skills=_as_list(j.get(JD_SOFT_KEY)),
        salary_min=int(j.get(JD_SALARY_MIN_KEY) or 0),
        salary_max=int(j.get(JD_SALARY_MAX_KEY) or 0),
        jd_text=_as_list(j.get(JD_TEXT_KEY)),
    )


# ── public entry point ────────────────────────────────────────────────────────

async def fetch_trigger_payload(
    candidate_id: str,
    job_id: str,
) -> TriggerInterviewRequest:
    """
    Pull candidate + resume + score + job from the ATS db and build the trigger
    request. Raises ATSDataError (with an HTTP status) on any recoverable problem
    so the endpoint can tell the ATS team exactly what went wrong:
      503 — ATS_DATABASE_URL not configured
      502 — ATS db read failed (unreachable, or schema placeholders not filled)
      404 — candidate not found
      422 — candidate has no email
    """
    factory = _get_ats_factory()
    if not factory:
        raise ATSDataError(503, "ATS_DATABASE_URL is not configured on the server")

    try:
        async with factory() as s:
            # 1 + 2 — candidate contact + parsed-resume JSON (one row).
            cand = (await s.execute(
                text(f"""
                    SELECT {CAND_NAME_COL}      AS name,
                           {CAND_EMAIL_COL}     AS email,
                           {CAND_PHONE_COL}     AS phone,
                           {CAND_RESUME_JSON}   AS parsed_resume,
                           {CAND_RESUME_FILENM} AS resume_filename
                    FROM   {CAND_TABLE}
                    WHERE  {CAND_ID_COL} = :cid
                """),
                {"cid": candidate_id},
            )).mappings().fetchone()

            # 3 — ATS score JSON for this candidate + job.
            score = (await s.execute(
                text(f"""
                    SELECT {SCORE_JSON_COL} AS ats_score
                    FROM   {SCORE_TABLE}
                    WHERE  {SCORE_CAND_COL} = :cid AND {SCORE_JOB_COL} = :jid
                """),
                {"cid": candidate_id, "jid": job_id},
            )).mappings().fetchone()

            # 4 — job / JD JSON.
            job = (await s.execute(
                text(f"""
                    SELECT {JOB_JSON_COL} AS jd
                    FROM   {JOB_TABLE}
                    WHERE  {JOB_ID_COL} = :jid
                """),
                {"jid": job_id},
            )).mappings().fetchone()
    except Exception as e:
        # Most likely: the schema placeholders aren't filled in yet, or the ATS
        # db is unreachable / a name is wrong. Surface a clear 502.
        logger.error(f"[ats-connector] ATS db read failed: {e}")
        raise ATSDataError(
            502,
            "ATS database read failed — check ATS_DATABASE_URL, the schema map "
            f"placeholders in ats_connector.py, and read access. ({e})",
        )

    if not cand:
        raise ATSDataError(404, f"candidate {candidate_id} not found in ATS database")

    # ── assemble the trigger body ─────────────────────────────────────────────
    parsed_resume = _as_json(cand["parsed_resume"]) or {}
    ats_score_data = _as_json(score["ats_score"]) if score else {}
    email = (cand["email"] or "").strip()

    if not email:
        # Hard requirement — the join link is emailed to the candidate.
        raise ATSDataError(
            422, f"candidate {candidate_id} has no email in ATS — cannot send invite"
        )

    resume_filename = _derive_filename(parsed_resume, cand["resume_filename"])

    payload = TriggerInterviewRequest(
        ats_candidate_id=str(candidate_id),
        candidate_name=str(cand["name"] or "Candidate"),
        candidate_email=email,
        candidate_phone=str(cand["phone"] or ""),
        resume_filename=resume_filename,
        parsed_resume=parsed_resume if isinstance(parsed_resume, dict) else {},
        ats_score_data=ats_score_data if isinstance(ats_score_data, dict) else {},
        job=_build_job_input(job["jd"] if job else {}, job_id),
    )
    logger.info(
        f"[ats-connector] built trigger payload for candidate={candidate_id} "
        f"job={job_id} email={email}"
    )
    return payload
