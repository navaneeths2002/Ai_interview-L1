"""
ATS Results Export
==================
After an interview completes (evaluation → report), flatten EVERYTHING into one
read-only row in `ats_interview_results`, keyed by the ATS's own ids. The ATS is
granted read-only access to that one table and pulls results by
(ats_candidate_id, ats_job_id) — the mirror of the trigger direction.

Fully self-contained and non-fatal: any failure is logged and swallowed so it
can never break the interview / report pipeline. Idempotent — re-running upserts
the same interview's row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.interview import (
    Interview, InterviewTranscript, InterviewExtractedData,
    InterviewScore, AtsInterviewResult,
)
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.report import InterviewReport

logger = logging.getLogger(__name__)


async def export_interview_results(interview_id: str) -> bool:
    """Build/refresh the ATS-facing results row for one interview. Never raises."""
    try:
        async with AsyncSessionLocal() as db:
            interview = (await db.execute(
                select(Interview).where(Interview.id == interview_id)
            )).scalar_one_or_none()
            if not interview:
                logger.warning(f"[ats-export] interview {interview_id} not found — skipping")
                return False

            candidate = (await db.execute(
                select(Candidate).where(Candidate.id == interview.candidate_id)
            )).scalar_one_or_none()
            job = (await db.execute(
                select(Job).where(Job.id == interview.job_id)
            )).scalar_one_or_none()
            score = (await db.execute(
                select(InterviewScore).where(InterviewScore.interview_id == interview_id)
            )).scalar_one_or_none()
            ext = (await db.execute(
                select(InterviewExtractedData).where(InterviewExtractedData.interview_id == interview_id)
            )).scalar_one_or_none()
            transcript = (await db.execute(
                select(InterviewTranscript).where(InterviewTranscript.interview_id == interview_id)
            )).scalar_one_or_none()
            report = (await db.execute(
                select(InterviewReport).where(InterviewReport.interview_id == interview_id)
            )).scalar_one_or_none()

            # raw_extraction holds the full Claude output: summary, strengths,
            # weaknesses, red_flags, extracted{}, and (for voice-heavy roles)
            # voice_analysis{}. Fall back gracefully if it's absent.
            raw = (ext.raw_extraction if ext and ext.raw_extraction else {}) or {}

            candidate_name = (
                f"{candidate.first_name} {candidate.last_name or ''}".strip()
                if candidate else None
            )

            # ── upsert the results row ────────────────────────────────────────
            row = (await db.execute(
                select(AtsInterviewResult).where(
                    AtsInterviewResult.interview_id == interview_id
                )
            )).scalar_one_or_none()
            if not row:
                row = AtsInterviewResult(
                    interview_id=interview_id,
                    tenant_id=str(interview.tenant_id),
                )
                db.add(row)

            row.ats_candidate_id = interview.ats_candidate_id
            row.ats_job_id       = interview.ats_job_id
            row.candidate_name   = candidate_name
            row.candidate_email  = candidate.email if candidate else None
            row.job_title        = job.position_title if job else None
            row.status           = interview.status

            if score:
                row.overall_score        = score.overall_score
                row.recommendation       = score.recruiter_override or score.recommendation
                row.communication_score  = score.communication_score
                row.confidence_score     = score.confidence_score
                row.jd_fit_score         = score.jd_fit_score
                row.behavioral_score     = score.behavioral_score
                row.salary_fit           = score.salary_fit
                row.experience_validated = score.experience_validated

            row.summary        = raw.get("summary") or (score.ai_reasoning if score else None)
            row.strengths      = raw.get("strengths") or []
            row.weaknesses     = raw.get("weaknesses") or []
            row.red_flags      = raw.get("red_flags") or []
            row.extracted      = (ext.extracted if ext else None) or raw.get("extracted") or {}
            row.voice_analysis = raw.get("voice_analysis")
            row.transcript     = (transcript.turns if transcript else None) or []

            if report:
                row.report_url  = report.report_url
                row.report_data = report.report_data
                row.report_html = report.report_html

            row.scheduled_at     = interview.scheduled_at
            row.started_at       = interview.started_at
            row.ended_at         = interview.ended_at
            row.duration_seconds = interview.duration_seconds
            row.exported_at      = datetime.now(timezone.utc)

            await db.commit()

        logger.info(
            f"[ats-export] ✓ results exported for {interview_id} "
            f"(ats_candidate={interview.ats_candidate_id}, ats_job={interview.ats_job_id})"
        )
        return True
    except Exception as e:
        logger.warning(f"[ats-export] export failed for {interview_id} (non-fatal): {e}")
        return False
