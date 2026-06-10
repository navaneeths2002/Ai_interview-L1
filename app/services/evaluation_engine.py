"""
Phase 6 — Post-Interview Evaluation Engine
============================================
Called automatically when interview.status → 'completed'.

Pipeline:
  1. Load full transcript + candidate profile + job + ATS score + interview context
  2. Build evaluation prompt
  3. Call Claude Sonnet → structured JSON scores
  4. Save to interview_scores + interview_extracted_data tables
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv
from sqlalchemy import select, and_

load_dotenv(override=True)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.interview import (
    Interview,
    InterviewContext,
    InterviewTranscript,
    InterviewExtractedData,
    InterviewScore,
)
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.ats_score import AtsScore

logger = logging.getLogger(__name__)

# Use the confirmed-working model on this account.
# Upgrade to claude-opus-4-5-20251001 when available for higher evaluation quality.
EVAL_MODEL = "claude-haiku-4-5-20251001"

# ── System prompt ──────────────────────────────────────────────────────────────

EVALUATION_SYSTEM_PROMPT = """\
You are a senior HR analyst reviewing a completed L1 screening interview transcript.

Evaluate the candidate objectively across five dimensions and extract structured data
from the conversation. You must return a single valid JSON object — no markdown fences,
no extra text, just the JSON.

Required JSON structure:
{
  "communication_score":    <integer 1-10>,
  "confidence_score":       <integer 1-10>,
  "jd_fit_score":           <integer 1-10>,
  "behavioral_score":       <integer 1-10>,
  "salary_fit":             <true | false | null>,
  "experience_validated":   <true | false>,
  "overall_score":          <integer 0-100>,
  "recommendation":         <"proceed_to_l2" | "hold" | "reject">,
  "strengths":              ["<point>", ...],
  "weaknesses":             ["<point>", ...],
  "red_flags":              ["<flag>" | empty list],
  "summary":                "<3-5 sentence recruiter-ready paragraph>",
  "extracted": {
    "current_company":        "<string | null>",
    "current_role":           "<string | null>",
    "total_experience_years": <number | null>,
    "current_ctc":            <integer annual salary in INR | null>,
    "expected_ctc":           <integer annual salary in INR | null>,
    "notice_period_days":     <integer | null>,
    "notice_negotiable":      <true | false | null>,
    "relocation_willing":     <true | false | null>,
    "preferred_locations":    ["<city>", ...],
    "work_authorization":     "<string | null>",
    "earliest_joining":       "<string | null>"
  }
}

Scoring guidelines:
  communication_score  — clarity, fluency, articulation (1=very poor, 10=excellent)
  confidence_score     — certainty, directness, lack of hesitation (1=very hesitant, 10=confident)
  jd_fit_score         — alignment with job requirements and critical skills (1=poor, 10=perfect)
  behavioral_score     — professionalism, stability signals, attitude (1=concerning, 10=exemplary)
  overall_score        — weighted composite:
                          (jd_fit × 35) + (communication × 25) + (behavioral × 15)
                          + (confidence × 15) + ats_boost (0–10 based on ATS pre-score)

Recommendation rules:
  proceed_to_l2 → overall_score ≥ 65  AND jd_fit ≥ 6  AND no critical red flags
  hold          → overall_score 50–64  OR borderline JD fit  OR one minor concern
  reject        → overall_score < 50   OR jd_fit < 4         OR critical red flags present

salary_fit: compare expected_ctc against job's salary_max. true if within range, false if over,
            null if either value is missing.

Return ONLY the JSON object.\
"""


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(
    candidate_name: str,
    profile: dict | None,          # Candidate.profile JSONB dict
    job: Job | None,
    ats: AtsScore | None,
    context: InterviewContext | None,
    transcripts: list,
) -> str:
    lines: list[str] = []
    p = profile or {}

    # ── Candidate profile ──────────────────────────────────────────────────────
    lines.append(f"## CANDIDATE: {candidate_name}")
    if p:
        if p.get("total_experience_years") is not None:
            lines.append(f"- Resume experience : {p['total_experience_years']} years")
        if p.get("current_company"):
            lines.append(f"- Current company   : {p['current_company']}")
        if p.get("current_role"):
            lines.append(f"- Current role      : {p['current_role']}")
        if p.get("skills"):
            # skills is a list of strings
            skills = [s if isinstance(s, str) else str(s) for s in p['skills']]
            lines.append(f"- Skills            : {', '.join(skills)}")
        if p.get("certifications"):
            # certifications can be a list of strings OR list of dicts
            # {"certification_name": "...", "issuing_organization": "..."}
            certs = []
            for c in p["certifications"]:
                if isinstance(c, str):
                    certs.append(c)
                elif isinstance(c, dict):
                    certs.append(c.get("certification_name") or c.get("name") or str(c))
            if certs:
                lines.append(f"- Certifications    : {', '.join(certs)}")

    # ── Job requirements ───────────────────────────────────────────────────────
    if job:
        lines.append(f"\n## JOB: {job.position_title}" +
                     (f" | {job.department}" if job.department else ""))
        if job.min_experience_years is not None:
            lines.append(f"- Min experience    : {job.min_experience_years} years")
        if job.critical_skills:
            lines.append(f"- Critical skills   : {', '.join(job.critical_skills)}")
        if job.optional_skills:
            lines.append(f"- Nice-to-have      : {', '.join(job.optional_skills)}")
        if job.salary_min and job.salary_max:
            lines.append(f"- Salary range      : ₹{job.salary_min:,} – ₹{job.salary_max:,}")
        if job.jd_text:
            lines.append(f"- JD summary        : {job.jd_text[:600]}")

    # ── ATS pre-assessment ─────────────────────────────────────────────────────
    if ats:
        lines.append(f"\n## ATS PRE-ASSESSMENT (score: {ats.total_score}/100)")
        if ats.strong_areas:
            lines.append(f"- Strong areas      : {', '.join(ats.strong_areas)}")
        if ats.missing_skills:
            lines.append(f"- Missing skills    : {', '.join(ats.missing_skills)}")
        if ats.risk_flags:
            lines.append(f"- Risk flags        : {', '.join(ats.risk_flags)}")

    # ── Interview strategy ─────────────────────────────────────────────────────
    if context:
        lines.append("\n## INTERVIEW FOCUS AREAS")
        if context.skills_to_validate:
            lines.append(f"- Skills validated  : {', '.join(context.skills_to_validate)}")
        if context.gaps_to_probe:
            lines.append(f"- Gaps probed       : {', '.join(context.gaps_to_probe)}")

    # ── Full transcript ────────────────────────────────────────────────────────
    # transcripts is now a list of dicts: {"speaker", "message", "spoken_at", "node"}
    lines.append(f"\n## INTERVIEW TRANSCRIPT  ({len(transcripts)} messages)\n")
    for t in transcripts:
        speaker = "AI" if t.get("speaker") == "ai" else candidate_name
        spoken_at_str = t.get("spoken_at", "")
        # ISO format: "2026-05-28T10:00:01.123456+00:00" → extract HH:MM:SS
        ts = spoken_at_str[11:19] if len(spoken_at_str) >= 19 else "--:--:--"
        lines.append(f"[{ts}] {speaker}: {t.get('message', '')}")

    return "\n".join(lines)


# ── JSON parser ────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Parse Claude's response — strips markdown fences if present."""
    raw = raw.strip()
    # Strip ```json ... ``` wrappers
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-resort: grab first { ... } block
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"No valid JSON in Claude response:\n{raw[:300]}")


# ── DB savers ──────────────────────────────────────────────────────────────────

def _safe_int(val) -> int | None:
    """Convert CTC / score values safely."""
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _build_reasoning(result: dict) -> str:
    """Combine summary + strengths/weaknesses/flags into the ai_reasoning text field."""
    parts = [result.get("summary", "")]
    strengths = result.get("strengths", [])
    weaknesses = result.get("weaknesses", [])
    red_flags = result.get("red_flags", [])
    if strengths:
        parts.append("\nSTRENGTHS:\n" + "\n".join(f"• {s}" for s in strengths))
    if weaknesses:
        parts.append("\nWEAKNESSES:\n" + "\n".join(f"• {w}" for w in weaknesses))
    if red_flags:
        parts.append("\nRED FLAGS:\n" + "\n".join(f"⚠ {f}" for f in red_flags))
    return "\n".join(parts)


async def _save_results(
    db: AsyncSession,
    interview: Interview,
    result: dict,
) -> None:
    interview_id = str(interview.id)
    tenant_id    = str(interview.tenant_id)
    ext          = result.get("extracted", {})

    # ── InterviewScore ─────────────────────────────────────────────────────────
    score_row = (await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )).scalar_one_or_none()

    if not score_row:
        score_row = InterviewScore(interview_id=interview_id, tenant_id=tenant_id)
        db.add(score_row)

    score_row.communication_score  = _safe_int(result.get("communication_score"))
    score_row.confidence_score     = _safe_int(result.get("confidence_score"))
    score_row.jd_fit_score         = _safe_int(result.get("jd_fit_score"))
    score_row.behavioral_score     = _safe_int(result.get("behavioral_score"))
    score_row.salary_fit           = result.get("salary_fit")
    score_row.experience_validated = result.get("experience_validated", False)
    score_row.overall_score        = _safe_int(result.get("overall_score"))
    score_row.recommendation       = result.get("recommendation", "hold")
    score_row.ai_reasoning         = _build_reasoning(result)

    # ── InterviewExtractedData ─────────────────────────────────────────────────
    ext_row = (await db.execute(
        select(InterviewExtractedData).where(InterviewExtractedData.interview_id == interview_id)
    )).scalar_one_or_none()

    if not ext_row:
        ext_row = InterviewExtractedData(interview_id=interview_id, tenant_id=tenant_id)
        db.add(ext_row)

    # Store the entire "extracted" block as a JSONB document — one write, all fields.
    # Coerce numeric types so the JSON is clean for downstream readers.
    ext_row.extracted = {
        "current_company":        ext.get("current_company"),
        "current_role":           ext.get("current_role"),
        "total_experience_years": _safe_float(ext.get("total_experience_years")),
        "current_ctc":            _safe_int(ext.get("current_ctc")),
        "expected_ctc":           _safe_int(ext.get("expected_ctc")),
        "notice_period_days":     _safe_int(ext.get("notice_period_days")),
        "notice_negotiable":      ext.get("notice_negotiable"),
        "relocation_willing":     ext.get("relocation_willing"),
        "preferred_locations":    ext.get("preferred_locations") or [],
        "work_authorization":     ext.get("work_authorization"),
        "earliest_joining":       ext.get("earliest_joining"),
    }
    # Keep the full raw Claude output for audit / re-processing
    ext_row.raw_extraction = result

    await db.commit()


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_evaluation(interview_id: str) -> bool:
    """
    Run post-interview evaluation for the given interview_id.

    Safe to call as a fire-and-forget asyncio task — creates its own
    DB session and handles all exceptions internally.

    Returns True on success, False on failure.
    """
    logger.info(f"[eval] starting evaluation for interview {interview_id}")
    try:
        async with AsyncSessionLocal() as db:
            return await _run(db, interview_id)
    except Exception as e:
        logger.error(f"[eval] fatal error for {interview_id}: {e}", exc_info=True)
        return False


async def _run(db: AsyncSession, interview_id: str) -> bool:
    # ── 1. Load interview ──────────────────────────────────────────────────────
    interview = (await db.execute(
        select(Interview).where(Interview.id == interview_id)
    )).scalar_one_or_none()
    if not interview:
        logger.error(f"[eval] interview {interview_id} not found")
        return False

    # ── 2. Load candidate (profile is embedded as JSONB) ──────────────────────
    candidate = (await db.execute(
        select(Candidate).where(Candidate.id == interview.candidate_id)
    )).scalar_one_or_none()

    candidate_name = (
        f"{candidate.first_name} {candidate.last_name or ''}".strip()
        if candidate else "Candidate"
    )

    # profile is now a plain dict from the JSONB column on Candidate
    profile: dict | None = candidate.profile if candidate else None

    # ── 3. Load job ────────────────────────────────────────────────────────────
    job = (await db.execute(
        select(Job).where(Job.id == interview.job_id)
    )).scalar_one_or_none()

    # ── 4. Load ATS score (keyed by candidate_id + job_id) ────────────────────
    ats = (await db.execute(
        select(AtsScore).where(and_(
            AtsScore.candidate_id == interview.candidate_id,
            AtsScore.job_id       == interview.job_id,
        ))
    )).scalar_one_or_none()

    # ── 5. Load interview context ──────────────────────────────────────────────
    context = (await db.execute(
        select(InterviewContext).where(InterviewContext.interview_id == interview_id)
    )).scalar_one_or_none()

    # ── 6. Load transcript (single row per interview, ordered JSONB array) ────
    transcript_row = (await db.execute(
        select(InterviewTranscript)
        .where(InterviewTranscript.interview_id == interview_id)
    )).scalar_one_or_none()

    turns = transcript_row.turns if transcript_row else []

    if not turns:
        logger.warning(f"[eval] no transcript found for {interview_id} — skipping")
        return False

    logger.info(f"[eval] loaded {len(turns)} transcript turns")

    # ── 7. Build prompt ────────────────────────────────────────────────────────
    user_prompt = _build_prompt(
        candidate_name=candidate_name,
        profile=profile,
        job=job,
        ats=ats,
        context=context,
        transcripts=turns,          # list of dicts {"speaker","message","spoken_at","node"}
    )

    # ── 8. Call Claude (with retry on transient errors) ────────────────────────
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    _retryable = (anthropic.APIStatusError, anthropic.APIConnectionError)
    _max_attempts = 3
    _backoff_seconds = [2, 4, 8]
    raw: str = ""

    for attempt in range(1, _max_attempts + 1):
        try:
            response = await client.messages.create(
                model=EVAL_MODEL,
                max_tokens=2048,
                system=EVALUATION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text
            break  # success
        except _retryable as exc:
            if attempt < _max_attempts:
                wait = _backoff_seconds[attempt - 1]
                logger.warning(
                    f"[eval] Claude API error on attempt {attempt}/{_max_attempts} "
                    f"for {interview_id} -- retrying in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    f"[eval] Claude API failed after {_max_attempts} attempts "
                    f"for {interview_id}: {exc}"
                )
                raise

    # ── 9. Parse JSON ──────────────────────────────────────────────────────────
    try:
        result = _parse_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error(f"[eval] JSON parse failed for {interview_id}: {e}")
        return False

    # ── 10. Override overall_score with exact formula ─────────────────────────
    # Claude approximates the calculation. We recompute it precisely so the
    # score is always mathematically correct regardless of Claude's rounding.
    # Weights: JD Fit 35% | Communication 25% | Behavioral 15% | Confidence 15% | ATS 10%
    try:
        ats_boost = (ats.total_score / 100) * 10 if ats else 0
        exact_score = round(
            (result.get("jd_fit_score",        0) * 35 +
             result.get("communication_score",  0) * 25 +
             result.get("behavioral_score",     0) * 15 +
             result.get("confidence_score",     0) * 15) / 10
            + ats_boost
        )
        result["overall_score"] = max(0, min(100, exact_score))
        logger.info(f"[eval] exact overall_score recalculated: {result['overall_score']}")
    except Exception as e:
        logger.warning(f"[eval] could not recalculate overall_score: {e}")

    # ── 11. Save to DB ─────────────────────────────────────────────────────────
    await _save_results(db, interview, result)

    logger.info(
        f"[eval] ✓ {interview_id} | "
        f"score={result.get('overall_score')} | "
        f"rec={result.get('recommendation')}"
    )

    # Phase 7: trigger report generation as follow-on background task
    try:
        from app.services.report_generator import run_report
        import asyncio
        asyncio.create_task(run_report(interview_id))
        logger.info(f"[report] generation queued for {interview_id}")
    except Exception as e:
        logger.warning(f"[report] could not queue generation: {e}")

    return True
