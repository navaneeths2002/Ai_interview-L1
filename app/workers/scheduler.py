"""
Phase 8 -- Workflow Durability: Background Scheduler
=====================================================
Uses APScheduler AsyncIOScheduler to run periodic recovery jobs.

Jobs:
  recover_stuck_interviews    -- every 15 minutes
  retry_missing_evaluations   -- every 10 minutes
  retry_missing_reports       -- every 10 minutes
  expire_abandoned_interviews -- every 60 minutes

Exposed:
  scheduler         -- APScheduler instance
  start_scheduler() -- call once on FastAPI startup
  stop_scheduler()  -- call once on FastAPI shutdown
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.recovery import (
    recover_stuck_interviews,
    retry_missing_evaluations,
    retry_missing_reports,
    expire_abandoned_interviews,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# Register jobs
scheduler.add_job(
    recover_stuck_interviews,
    trigger=IntervalTrigger(minutes=15),
    id="recover_stuck_interviews",
    name="Recover stuck in-progress interviews",
    replace_existing=True,
    misfire_grace_time=120,
    coalesce=True,
)

scheduler.add_job(
    retry_missing_evaluations,
    trigger=IntervalTrigger(minutes=10),
    id="retry_missing_evaluations",
    name="Retry evaluations for completed interviews with no score",
    replace_existing=True,
    misfire_grace_time=120,
    coalesce=True,
)

scheduler.add_job(
    retry_missing_reports,
    trigger=IntervalTrigger(minutes=10),
    id="retry_missing_reports",
    name="Retry reports for scored interviews with no report",
    replace_existing=True,
    misfire_grace_time=120,
    coalesce=True,
)

scheduler.add_job(
    expire_abandoned_interviews,
    trigger=IntervalTrigger(minutes=60),
    id="expire_abandoned_interviews",
    name="Expire scheduled interviews never started after 24h",
    replace_existing=True,
    misfire_grace_time=300,
    coalesce=True,
)


def start_scheduler() -> None:
    """Start the background scheduler. Safe to call multiple times."""
    if not scheduler.running:
        scheduler.start()
        logger.info("[scheduler] started (4 recovery jobs registered)")
    else:
        logger.warning("[scheduler] start_scheduler called but scheduler already running")


def stop_scheduler() -> None:
    """Gracefully stop the scheduler on FastAPI shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[scheduler] stopped")
