"""
ATS Connector — single call, single table
=========================================
The ATS grants us READ-ONLY access to exactly one consolidated table,
`AiInterviewScheduleDetails`, which holds everything we need as three JSON
columns. The ATS calls our integration endpoint with {candidate_id, job_id};
we read that one row and assemble a TriggerInterviewRequest.

    mysql+aiomysql://user:password@host:3306/dbname   (ATS_DATABASE_URL)

────────────────────────────────────────────────────────────────────────────────
TABLE: AiInterviewScheduleDetails  (verified)
  candidate_id        varchar   — the id the ATS sends
  job_id              varchar   — the id the ATS sends
  ResumeParseData     json      — raw /parse output (resume object directly)
  ScoreJsonData       json      — raw /ats-score result (single result object)
  JobDetailsJsonData  json      — job basics {ID, JOB_TITLE, JOB_DESCRIPTION, REQUIREMENTS, ...}
  UrlExpiryTime, CreatedDate, CreatedBy — metadata (unused)
────────────────────────────────────────────────────────────────────────────────
Two shape fixes the extractors require:
  • parsed_resume  — the column stores the resume object DIRECTLY, but
    extract_resume_data() does next(iter(parsed_resume.values())), so we WRAP it
    as { filename: ResumeParseData }.
  • ats_score_data — the column stores a single result, but extract_ats_data()
    reads {"results": [{"file": ..., "result": {...}}]}, so we WRAP it likewise.

Candidate contact (name / email / phone) lives INSIDE ResumeParseData — there
are no separate contact columns.

NOTE: JobDetailsJsonData holds only job basics — it does NOT carry required
skills, salary, or min-experience. Those come through empty (the interview still
runs; JD-alignment/skill-gap probing is just lighter). If richer JD-aware
interviews are wanted, ask the ATS to add those fields to JobDetailsJsonData.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

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

# The one table we're granted read-only access to.
ATS_TABLE = "AiInterviewScheduleDetails"

# ── keys INSIDE JobDetailsJsonData (verified against the ATS's actual JSON) ──────
# The ATS uses CamelCase keys (JobTitle / JobDescription / City …), NOT the
# SCREAMING_CASE originally assumed — the old keys silently returned empty, so every
# interview ran with a blank job title / description / location.
JD_TITLE_KEY = "JobTitle"
JD_DESC_KEY  = "JobDescription"
JD_DEPT_KEY  = "Department"
JD_CITY_KEY  = "City"
JD_CRIT_KEY  = "CriticalSkills"
JD_OPT_KEY   = "OptionalSkills"
JD_SOFT_KEY  = "SoftSkills"
JD_EXP_KEY   = "Experience"

# Client domain URL → tenant. The ATS supplies a client domain URL to identify the
# organisation (replacing the earlier ORG_ID plan). Exact field name is not yet
# finalised, so we check several likely keys — CONFIRM THE KEY WITH THE ATS TEAM.
JD_DOMAIN_KEYS = (
    "ClientDomainUrl", "ClientDomainURL", "ClientDomain",
    "ClientUrl", "DomainUrl", "client_domain_url", "client_domain", "domain_url",
)


class ATSDataError(Exception):
    """
    Raised when ATS data can't be turned into a trigger payload. Carries an HTTP
    status so the endpoint surfaces a CLEAR reason to the ATS team:
      503 — ATS_DATABASE_URL not configured
      502 — ATS db read failed (unreachable / bad credentials)
      404 — no schedule row for that candidate + job
      422 — candidate has no email in the resume JSON
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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
        logger.info("[ats-connector] read-only ATS (MySQL) engine created")
    return _ats_factory


# ── helpers ───────────────────────────────────────────────────────────────────

def _as_json(value: Any) -> Any:
    """MySQL JSON may arrive as a str — decode to dict/list if so."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _split_skills(value: Any) -> list[str]:
    """Skills arrive as a delimited string ('Python, Docker; SQL') or a list."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    s = _clean(value)
    if not s:
        return []
    return [p.strip() for p in re.split(r"[,;|\n]+", s) if p.strip()]


def _parse_years(value: Any) -> int:
    """Pull a whole-number year count out of an Experience string ('3', '3 yrs')."""
    m = re.search(r"\d+(?:\.\d+)?", _clean(value))
    return int(float(m.group())) if m else 0


def _strip_html(value: Any) -> str:
    """JobDescription is HTML — reduce it to clean text for the strategy prompt."""
    s = _clean(value)
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


def _normalize_domain_to_tenant(raw: Any) -> str:
    """
    Normalise a client domain URL into a valid tenant id (host only). Must satisfy
    TenantMiddleware's ^[a-zA-Z0-9_\\-\\.]{3,64}$ — strips scheme / path / 'www.'.
    e.g. 'https://acme.hrms.com/portal' → 'acme.hrms.com'.
    """
    s = _clean(raw)
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    try:
        host = (urlparse(s).hostname or "").lower().strip()
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(r"[^a-z0-9_.\-]", "", host)
    return host[:64]


def _derive_tenant_id(job: dict) -> str:
    """
    Tenant = the ATS client, identified by their client domain URL (e.g.
    'acme.hrms.com'), normalised to a valid tenant id so each client's interview
    data stays grouped. Read from any of JD_DOMAIN_KEYS. Falls back to 'ats-default'.
    """
    job = job or {}
    for k in JD_DOMAIN_KEYS:
        tenant = _normalize_domain_to_tenant(job.get(k))
        if len(tenant) >= 3:            # TenantMiddleware requires 3–64 chars
            logger.info(f"[ats-connector] tenant from client domain '{k}' → {tenant}")
            return tenant
    logger.warning("[ats-connector] no client domain url in job details — tenant=ats-default")
    return "ats-default"


def _build_job_input(job: dict, ats_job_id: str) -> JobInput:
    """Map JobDetailsJsonData → JobInput using the ATS's real key names."""
    job = job if isinstance(job, dict) else {}
    desc = _strip_html(job.get(JD_DESC_KEY))
    return JobInput(
        ats_job_id=str(ats_job_id),
        position_title=_clean(job.get(JD_TITLE_KEY)),
        department=_clean(job.get(JD_DEPT_KEY)),
        location=_clean(job.get(JD_CITY_KEY)),
        position_type="full_time",
        min_experience_years=_parse_years(job.get(JD_EXP_KEY)),
        critical_skills=_split_skills(job.get(JD_CRIT_KEY)),
        optional_skills=_split_skills(job.get(JD_OPT_KEY)),
        soft_skills=_split_skills(job.get(JD_SOFT_KEY)),
        salary_min=0,
        salary_max=0,
        jd_text=[desc] if desc else [],
    )


# ── public entry point ────────────────────────────────────────────────────────

async def fetch_trigger_payload(
    candidate_id: str,
    job_id: str,
) -> tuple[TriggerInterviewRequest, str]:
    """
    Read the one AiInterviewScheduleDetails row for (candidate_id, job_id) and
    build the trigger request. Returns (payload, tenant_id) where tenant_id is
    derived from the ATS ORG_ID. Raises ATSDataError (with an HTTP status) on any
    recoverable problem.
    """
    factory = _get_ats_factory()
    if not factory:
        raise ATSDataError(503, "ATS_DATABASE_URL is not configured on the server")

    try:
        async with factory() as s:
            row = (await s.execute(
                text(f"""
                    SELECT ResumeParseData, ScoreJsonData, JobDetailsJsonData
                    FROM   {ATS_TABLE}
                    WHERE  candidate_id = :cid AND job_id = :jid
                    ORDER BY CreatedDate DESC
                    LIMIT 1
                """),
                {"cid": str(candidate_id), "jid": str(job_id)},
            )).mappings().fetchone()
    except Exception as e:
        logger.error(f"[ats-connector] ATS db read failed: {e}")
        raise ATSDataError(
            502,
            "ATS database read failed — check ATS_DATABASE_URL (host/credentials), "
            f"read access to {ATS_TABLE}, and network reachability. ({e})",
        )

    if not row:
        raise ATSDataError(
            404,
            f"No Details found for Candidate ID:{candidate_id}",
        )

    resume = _as_json(row["ResumeParseData"]) or {}
    score  = _as_json(row["ScoreJsonData"]) or {}
    job    = _as_json(row["JobDetailsJsonData"]) or {}
    if not isinstance(resume, dict):
        resume = {}

    # ── candidate contact — lives inside the resume JSON ──────────────────────
    email = _clean(resume.get("email"))
    if not email:
        raise ATSDataError(
            422,
            f"candidate {candidate_id} has no email in ResumeParseData — cannot invite",
        )
    name = " ".join(
        p for p in (_clean(resume.get("first_name")), _clean(resume.get("last_name"))) if p
    )
    phone = _clean(resume.get("phone"))

    # filename — used to match resume ↔ score; take it from the parse Logs.
    filename = _clean((resume.get("Logs") or {}).get("filename")) or "resume.pdf"

    # ── WRAP for the extractors ───────────────────────────────────────────────
    parsed_resume = {filename: resume}
    ats_score_data = (
        {"results": [{"file": filename, "result": score}]}
        if isinstance(score, dict) and score else {}
    )
    if not ats_score_data:
        logger.warning(
            f"[ats-connector] no ATS score in row for candidate={candidate_id} "
            f"job={job_id} — proceeding with empty score"
        )

    payload = TriggerInterviewRequest(
        ats_candidate_id=str(candidate_id),
        candidate_name=name or "Candidate",
        candidate_email=email,
        candidate_phone=phone,
        resume_filename=filename,
        parsed_resume=parsed_resume,
        ats_score_data=ats_score_data,
        job=_build_job_input(job, job_id),
    )
    tenant_id = _derive_tenant_id(job)
    logger.info(
        f"[ats-connector] built trigger payload from {ATS_TABLE} — "
        f"candidate={candidate_id} job={job_id} name='{name}' email={email} "
        f"tenant={tenant_id}"
    )
    return payload, tenant_id
