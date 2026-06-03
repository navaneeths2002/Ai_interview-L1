"""
Phase 8 -- Workflow Durability: Recovery Service
=================================================
Handles interviews that were left in a broken state due to agent worker crashes
(Ctrl+C, OOM kills, network splits) where on_shutdown never fired.

Four recovery functions:
  recover_stuck_interviews()   -- in_progress > 2 hours -> mark completed + re-evaluate
  retry_missing_evaluations()  -- completed but no interview_scores row
  retry_missing_reports()      -- completed + has scores but no interview_reports row
  expire_abandoned_interviews()-- scheduled > 24 hours with 0 transcript rows -> expired

All functions are safe to call concurrently -- each opens its own DB session.
run_all_recovery() sequences them and returns a summary dict.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, text, update, and_, or_, not_, exists, Integer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.interview import (
    Interview,
    InterviewScore,
    InterviewTranscript,
)
from app.models.report import InterviewReport
from app.services.evaluation_engine import run_evaluation
# run_report is NOT imported here — run_evaluation chains it internally.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. recover_stuck_interviews
# ---------------------------------------------------------------------------

async def recover_stuck_interviews() -> int:
    """
    Find interviews stuck as 'in_progress' for more than 2 hours.
    Mark them completed, estimate duration, then run evaluation + report.
    Returns the count of interviews recovered.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    recovered = 0

    try:
        async with AsyncSessionLocal() as db:
            # Find all stuck interviews
            rows = (await db.execute(
                select(Interview).where(
                    and_(
                        Interview.status == "in_progress",
                        Interview.started_at < cutoff,
                    )
                )
            )).scalars().all()

            if not rows:
                logger.info("[recovery] recover_stuck_interviews: none found")
                return 0

            logger.info(
                f"[recovery] recover_stuck_interviews: found {len(rows)} stuck interview(s)"
            )

            now = datetime.now(timezone.utc)
            ids_to_process = []

            for interview in rows:
                # Estimate duration from transcript timestamps if available.
                # New design: one row per interview, turns is an ordered JSONB array.
                transcript_row = (await db.execute(
                    select(InterviewTranscript).where(
                        InterviewTranscript.interview_id == str(interview.id)
                    )
                )).scalar_one_or_none()

                first_ts = None
                last_ts  = None
                if transcript_row and transcript_row.turns:
                    try:
                        first_str = transcript_row.turns[0].get("spoken_at")
                        last_str  = transcript_row.turns[-1].get("spoken_at")
                        if first_str:
                            first_ts = datetime.fromisoformat(
                                first_str.replace("Z", "+00:00")
                            )
                        if last_str:
                            last_ts = datetime.fromisoformat(
                                last_str.replace("Z", "+00:00")
                            )
                    except Exception:
                        pass

                duration = None
                if first_ts and last_ts and last_ts > first_ts:
                    duration = int((last_ts - first_ts).total_seconds())
                elif interview.started_at:
                    # Fall back: time since start capped at 3600 s
                    elapsed = int((now - interview.started_at).total_seconds())
                    duration = min(elapsed, 3600)

                interview.status = "completed"
                interview.ended_at = now
                if duration is not None:
                    interview.duration_seconds = duration

                ids_to_process.append(str(interview.id))
                recovered += 1

            await db.commit()

        # Run evaluation outside the commit session.
        # run_evaluation already chains run_report internally — do NOT call
        # run_report separately here or the report will be generated twice (BUG 15).
        for interview_id in ids_to_process:
            try:
                logger.info(
                    f"[recovery] running evaluation for recovered interview {interview_id}"
                )
                await run_evaluation(interview_id)
            except Exception as exc:
                logger.error(
                    f"[recovery] evaluation failed for {interview_id}: {exc}",
                    exc_info=True,
                )

        logger.info(f"[recovery] recover_stuck_interviews: recovered {recovered}")

    except Exception as exc:
        logger.error(
            f"[recovery] recover_stuck_interviews fatal: {exc}", exc_info=True
        )

    return recovered


# ---------------------------------------------------------------------------
# 2. retry_missing_evaluations
# ---------------------------------------------------------------------------

async def retry_missing_evaluations() -> int:
    """
    Find completed interviews that have no row in interview_scores.
    Re-run evaluation for each.
    Returns count retried.
    """
    retried = 0

    try:
        async with AsyncSessionLocal() as db:
            # Interviews that are completed but have no InterviewScore row
            rows = (await db.execute(
                select(Interview).where(
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
            )).scalars().all()

            if not rows:
                logger.info("[recovery] retry_missing_evaluations: none found")
                return 0

            logger.info(
                f"[recovery] retry_missing_evaluations: found {len(rows)} interview(s)"
            )
            ids = [str(r.id) for r in rows]

        for interview_id in ids:
            try:
                logger.info(
                    f"[recovery] retrying evaluation for {interview_id}"
                )
                await run_evaluation(interview_id)
                retried += 1
            except Exception as exc:
                logger.error(
                    f"[recovery] evaluation retry failed for {interview_id}: {exc}",
                    exc_info=True,
                )

        logger.info(f"[recovery] retry_missing_evaluations: retried {retried}")

    except Exception as exc:
        logger.error(
            f"[recovery] retry_missing_evaluations fatal: {exc}", exc_info=True
        )

    return retried


# ---------------------------------------------------------------------------
# 3. retry_missing_reports
# ---------------------------------------------------------------------------

async def retry_missing_reports() -> int:
    """
    Find completed interviews that have an InterviewScore row but no
    InterviewReport row.  Re-run report generation for each.
    Returns count retried.
    """
    retried = 0

    try:
        async with AsyncSessionLocal() as db:
            # Completed + has scores + missing report
            rows = (await db.execute(
                select(Interview).where(
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
            )).scalars().all()

            if not rows:
                logger.info("[recovery] retry_missing_reports: none found")
                return 0

            logger.info(
                f"[recovery] retry_missing_reports: found {len(rows)} interview(s)"
            )
            ids = [str(r.id) for r in rows]

        for interview_id in ids:
            try:
                logger.info(
                    f"[recovery] retrying report for {interview_id}"
                )
                await run_report(interview_id)
                retried += 1
            except Exception as exc:
                logger.error(
                    f"[recovery] report retry failed for {interview_id}: {exc}",
                    exc_info=True,
                )

        logger.info(f"[recovery] retry_missing_reports: retried {retried}")

    except Exception as exc:
        logger.error(
            f"[recovery] retry_missing_reports fatal: {exc}", exc_info=True
        )

    return retried


# ---------------------------------------------------------------------------
# 4. expire_abandoned_interviews
# ---------------------------------------------------------------------------

async def expire_abandoned_interviews() -> int:
    """
    Find interviews that are still 'scheduled' after 24 hours and have
    0 transcript rows (candidate never joined).  Mark them 'expired'.
    Returns count expired.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = 0

    try:
        async with AsyncSessionLocal() as db:
            # Scheduled interviews older than 24 h with no transcript turns.
            # Use scheduled_at when set (more accurate); fall back to created_at.
            rows = (await db.execute(
                select(Interview).where(
                    and_(
                        Interview.status == "scheduled",
                        or_(
                            and_(Interview.scheduled_at.isnot(None), Interview.scheduled_at < cutoff),
                            and_(Interview.scheduled_at.is_(None),   Interview.created_at  < cutoff),
                        ),
                        not_(
                            exists(
                                select(InterviewTranscript.interview_id).where(
                                    and_(
                                        InterviewTranscript.interview_id == Interview.id,
                                        InterviewTranscript.turn_count > 0,
                                    )
                                )
                            )
                        ),
                    )
                )
            )).scalars().all()

            if not rows:
                logger.info("[recovery] expire_abandoned_interviews: none found")
                return 0

            logger.info(
                f"[recovery] expire_abandoned_interviews: found {len(rows)} interview(s)"
            )

            now = datetime.now(timezone.utc)
            for interview in rows:
                interview.status = "expired"
                interview.ended_at = now
                expired += 1

            await db.commit()

        logger.info(f"[recovery] expire_abandoned_interviews: expired {expired}")

    except Exception as exc:
        logger.error(
            f"[recovery] expire_abandoned_interviews fatal: {exc}", exc_info=True
        )

    return expired


# ---------------------------------------------------------------------------
# 5. run_all_recovery  (public entry point)
# ---------------------------------------------------------------------------

async def run_all_recovery() -> dict:
    """
    Run all four recovery functions in sequence.
    One failure does not block the others.
    Returns a summary dict with per-function counts.
    """
    summary: dict = {
        "stuck_recovered":         0,
        "evaluations_retried":     0,
        "reports_retried":         0,
        "abandoned_expired":       0,
        "errors":                  [],
    }

    for fn, key in [
        (recover_stuck_interviews,    "stuck_recovered"),
        (retry_missing_evaluations,   "evaluations_retried"),
        (retry_missing_reports,       "reports_retried"),
        (expire_abandoned_interviews, "abandoned_expired"),
    ]:
        try:
            summary[key] = await fn()
        except Exception as exc:
            msg = f"{fn.__name__}: {exc}"
            logger.error(f"[recovery] run_all_recovery caught error: {msg}", exc_info=True)
            summary["errors"].append(msg)

    logger.info(f"[recovery] run_all_recovery complete: {summary}")
    return summary
