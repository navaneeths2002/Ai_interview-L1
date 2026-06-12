"""
Crash Recovery & Reconnection Unit
===================================
Self-contained module — ALL reload/crash resilience logic lives here.
The agent only calls thin hooks; no agent pipeline internals are touched.

Three capabilities:

  1. DisconnectGraceTimer
     60-second grace period when the candidate disconnects (page reload,
     network blip). The interview is finalized ONLY if they don't return
     within the grace window. Reconnect cancels the countdown.

  2. queue_save_stage() / load_resume_state()
     After every conversation turn the current LangGraph stage is persisted
     to interview_contexts.question_flow (existing JSONB column — no
     migration needed). If the agent process crashes, the stage survives.

  3. apply_resume() / resume_greeting_instructions()
     When a fresh agent job starts for an interview already 'in_progress'
     with a saved stage, the LangGraph state is restored to that stage and
     Sarah re-greets with "Welcome back…" instead of starting over.

This module manages its own small DB engine (pool_size=2) so it never
competes with or depends on the agent's DB internals.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Awaitable, Callable

from dotenv import load_dotenv
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

# Load .env by absolute path — same safety as the agent (works from any cwd)
_env_path = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

GRACE_PERIOD_SECONDS = 60.0

# Keys of the LangGraph capture flags we persist/restore
_CAPTURE_KEYS = [
    "captured_intro",
    "captured_experience",
    "captured_current_ctc",
    "captured_expected_ctc",
    "captured_notice_period",
    "captured_relocation",
    "captured_joining",
]

# Human-readable topic per stage — used in the "Welcome back" re-greet
_STAGE_TOPIC = {
    "intro":         "a brief introduction about themselves",
    "experience":    "their total years of professional experience",
    "current_ctc":   "their current CTC (annual salary)",
    "expected_ctc":  "their expected salary for this role",
    "notice_period": "their notice period",
    "relocation":    "whether they are open to relocating",
    "joining":       "their earliest joining date",
    "wrap_up":       "wrapping up the interview",
}


# ── Own tiny DB engine (independent of the agent's) ────────────────────────────

_engine = None
_session_factory = None


def _get_factory():
    global _engine, _session_factory
    if _session_factory is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            logger.error("[crash-recovery] DATABASE_URL empty — stage persistence disabled")
            return None
        _engine = create_async_engine(db_url, pool_size=2, max_overflow=3, pool_pre_ping=True)
        _session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)
    return _session_factory


# ════════════════════════════════════════════════════════════════════════════════
# 1. Disconnect grace period
# ════════════════════════════════════════════════════════════════════════════════

class DisconnectGraceTimer:
    """
    Delays interview finalization when the candidate disconnects.

    candidate_left()      → starts a countdown (default 60 s)
    candidate_returned()  → cancels it; returns True if this was a reconnect
    countdown expiry      → calls on_expired() (the real shutdown path)
    """

    def __init__(
        self,
        on_expired: Callable[[], Awaitable[None]],
        grace_seconds: float = GRACE_PERIOD_SECONDS,
        interview_id: str | None = None,
    ) -> None:
        self._on_expired   = on_expired
        self._grace        = grace_seconds
        self._interview_id = interview_id
        self._task: asyncio.Task | None = None

    def candidate_left(self) -> None:
        """Candidate disconnected — start (or restart) the grace countdown."""
        self.cancel()
        self._task = asyncio.create_task(self._countdown(), name="disconnect-grace")
        logger.info(
            f"[crash-recovery] candidate disconnected — {self._grace:.0f}s grace "
            "period started (reload-safe, interview NOT finalized yet)",
            extra={"interview_id": self._interview_id},
        )

    def candidate_returned(self) -> bool:
        """
        Candidate (re)connected.
        Returns True if a grace countdown was active — i.e. this is a
        reconnect of an interrupted session, so Sarah should re-greet.
        """
        was_waiting = self._task is not None and not self._task.done()
        self.cancel()
        if was_waiting:
            logger.info(
                "[crash-recovery] candidate RECONNECTED within grace period — "
                "interview continues from where it stopped",
                extra={"interview_id": self._interview_id},
            )
        return was_waiting

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _countdown(self) -> None:
        try:
            await asyncio.sleep(self._grace)
        except asyncio.CancelledError:
            return  # candidate came back — normal path
        logger.info(
            f"[crash-recovery] grace period ({self._grace:.0f}s) expired — "
            "candidate did not return, finalizing interview",
            extra={"interview_id": self._interview_id},
        )
        try:
            await self._on_expired()
        except Exception as e:
            logger.error(f"[crash-recovery] on_expired callback failed: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════════════════════════
# 2. Stage persistence (crash-proof)
# ════════════════════════════════════════════════════════════════════════════════

async def _save_stage(interview_id: str, graph_state: dict) -> None:
    factory = _get_factory()
    if not factory:
        return

    payload = {
        "resume": {
            "stage":          graph_state.get("stage", "intro"),
            "turns_in_stage": graph_state.get("turns_in_stage", 0),
            "captured":       {k: bool(graph_state.get(k)) for k in _CAPTURE_KEYS},
            "saved_at":       datetime.now(timezone.utc).isoformat(),
        }
    }

    try:
        async with factory() as session:
            await session.execute(
                text("""
                    UPDATE interview_contexts
                    SET    question_flow = :payload,
                           updated_at    = :now
                    WHERE  interview_id  = :interview_id
                """).bindparams(bindparam("payload", type_=PG_JSONB)),
                {
                    "payload":      payload,
                    "now":          datetime.now(timezone.utc),
                    "interview_id": interview_id,
                },
            )
            await session.commit()
    except Exception as e:
        # Never let persistence failures affect the live interview
        logger.warning(f"[crash-recovery] stage save failed (non-fatal): {e}")


def queue_save_stage(interview_id: str | None, graph_state: dict) -> None:
    """
    Fire-and-forget stage persistence — call after every LangGraph advance.
    Safe no-op if interview_id is missing.
    """
    if not interview_id:
        return
    asyncio.create_task(_save_stage(interview_id, dict(graph_state)))


# ════════════════════════════════════════════════════════════════════════════════
# 3. Resume on fresh agent job (after agent crash)
# ════════════════════════════════════════════════════════════════════════════════

async def load_resume_state(interview_id: str) -> dict | None:
    """
    Returns the saved resume payload {"stage", "turns_in_stage", "captured"}
    if — and only if — the interview is still 'in_progress' and a meaningful
    stage was saved. Returns None for fresh interviews (start normally).
    """
    factory = _get_factory()
    if not factory:
        return None

    try:
        async with factory() as session:
            row = (await session.execute(
                text("""
                    SELECT i.status, c.question_flow
                    FROM interviews i
                    LEFT JOIN interview_contexts c ON c.interview_id = i.id
                    WHERE i.id = :id
                """),
                {"id": interview_id},
            )).fetchone()

        if not row or row[0] != "in_progress":
            return None

        resume = (row[1] or {}).get("resume")
        if not resume:
            return None

        stage = resume.get("stage")
        # Nothing meaningful to resume at intro/complete — start normally
        if stage in (None, "", "intro", "complete"):
            return None

        logger.info(
            f"[crash-recovery] found resumable interview — stage='{stage}', "
            f"saved_at={resume.get('saved_at')}",
            extra={"interview_id": interview_id},
        )
        return resume

    except Exception as e:
        logger.warning(f"[crash-recovery] load_resume_state failed (starting fresh): {e}")
        return None


def apply_resume(graph_state: dict, resume: dict) -> dict:
    """
    Restore a LangGraph state dict to the saved stage.
    Regenerates the stage_instruction so the LLM focuses on the right topic.
    """
    # Import here (read-only use) — keeps this module decoupled at import time
    from app.realtime.interview_graph import _make_instruction

    graph_state["stage"]          = resume.get("stage", "intro")
    graph_state["turns_in_stage"] = resume.get("turns_in_stage", 0)
    for key, value in (resume.get("captured") or {}).items():
        if key in graph_state:
            graph_state[key] = bool(value)

    graph_state["stage_instruction"] = _make_instruction(graph_state["stage"], graph_state)
    return graph_state


def resume_greeting_instructions(candidate_name: str, stage: str) -> str:
    """The 'Welcome back…' re-greet Sarah speaks when an interview resumes."""
    topic = _STAGE_TOPIC.get(stage, "where you left off")
    name  = candidate_name or "the candidate"
    return (
        f"The candidate {name} was in the middle of this screening interview when the "
        "connection was interrupted, and they have just reconnected. "
        f"Warmly welcome them back — say something like 'Welcome back, {name}! "
        "Glad to have you again — no worries, we'll continue right where we left off.' "
        f"Then naturally re-ask the question about {topic}. "
        "Do NOT restart the interview or re-introduce yourself. One question only."
    )
