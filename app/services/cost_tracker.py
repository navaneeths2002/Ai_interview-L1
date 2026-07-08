"""
Per-interview cost tracker
==========================
Self-contained unit (own small DB engine) that records the REAL usage of an
interview across processes and finalizes the cost at the end.

Three writers patch the same `interview_costs` row (merged via JSONB ||):
  • context_builder   → strategy_in / strategy_out      (trigger, FastAPI process)
  • agent.on_shutdown → llm_in/out, tts_chars, stt_seconds, duration, avatar  (worker)
  • evaluation_engine → eval_in / eval_out              (post-interview)

finalize_and_log() reads the merged usage, computes cost via pricing.compute_cost,
saves the breakdown, and prints it to the terminal. All failures are non-fatal —
cost tracking must never break a live interview.
"""

from __future__ import annotations

import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services import pricing

logger = logging.getLogger(__name__)

_env_path = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

_engine = None
_factory = None


def _get_factory():
    global _engine, _factory
    if _factory is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            logger.error("[cost] DATABASE_URL empty — cost tracking disabled")
            return None
        _engine = create_async_engine(db_url, pool_size=2, max_overflow=3, pool_pre_ping=True)
        _factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)
    return _factory


async def patch_usage(interview_id: str | None, tenant_id: str | None, patch: dict) -> None:
    """Merge `patch` into the interview's usage JSONB (upsert). Fire-and-forget safe."""
    if not interview_id or not patch:
        return
    factory = _get_factory()
    if not factory:
        return
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO interview_costs
                        (id, tenant_id, interview_id, usage, created_at, updated_at)
                    VALUES
                        (:id, :tenant_id, :interview_id, :patch, :now, :now)
                    ON CONFLICT (interview_id) DO UPDATE SET
                        usage      = COALESCE(interview_costs.usage, :empty) || :patch,
                        updated_at = :now
                """).bindparams(
                    bindparam("patch", type_=PG_JSONB),
                    bindparam("empty", type_=PG_JSONB),
                ),
                {
                    "id":           str(uuid.uuid4()),
                    "tenant_id":    tenant_id or "unknown",
                    "interview_id": interview_id,
                    "patch":        patch,
                    "empty":        {},
                    "now":          now,
                },
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[cost] patch_usage failed (non-fatal): {e}")


async def finalize_and_log(interview_id: str | None) -> dict | None:
    """
    Read the merged usage, compute the cost breakdown, save it, and print it to
    the terminal. Returns the cost dict (or None on failure).
    """
    if not interview_id:
        return None
    factory = _get_factory()
    if not factory:
        return None
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            row = (await session.execute(
                text("SELECT usage FROM interview_costs WHERE interview_id = :id"),
                {"id": interview_id},
            )).fetchone()

            usage = (row[0] if row else None) or {}
            cost  = pricing.compute_cost(usage)

            await session.execute(
                text("""
                    UPDATE interview_costs
                    SET    cost = :cost, total_usd = :total, updated_at = :now
                    WHERE  interview_id = :id
                """).bindparams(bindparam("cost", type_=PG_JSONB)),
                {"cost": cost, "total": cost["total_usd"], "now": now, "id": interview_id},
            )
            await session.commit()

        _log_breakdown(interview_id, usage, cost)
        return cost
    except Exception as e:
        logger.warning(f"[cost] finalize failed (non-fatal): {e}")
        return None


def _log_breakdown(interview_id: str, usage: dict, cost: dict) -> None:
    """Print a readable cost breakdown to the terminal at interview end."""
    tok_in  = sum(float(usage.get(k) or 0) for k in ("llm_in", "eval_in", "strategy_in", "voice_in"))
    tok_out = sum(float(usage.get(k) or 0) for k in ("llm_out", "eval_out", "strategy_out", "voice_out"))
    dur     = float(usage.get("duration_seconds") or 0)
    logger.info(
        "[cost] ====== INTERVIEW COST ======\n"
        f"        interview : {interview_id}\n"
        f"        duration  : {dur/60:.1f} min\n"
        f"        Claude    : ${cost['claude']:.4f}  ({tok_in:,.0f} in / {tok_out:,.0f} out tokens)\n"
        f"        TTS(Aura-2): ${cost['elevenlabs']:.4f}  ({float(usage.get('tts_chars') or 0):,.0f} chars)\n"
        f"        Deepgram  : ${cost['deepgram']:.4f}  ({float(usage.get('stt_seconds') or 0):,.0f} s)\n"
        f"        LiveKit   : ${cost['livekit']:.4f}\n"
        f"        Simli     : ${cost['simli']:.4f}  ({float(usage.get('avatar_seconds') or 0)/60:.1f} min)\n"
        f"        --------------------------------\n"
        f"        TOTAL     : ${cost['total_usd']:.4f}\n"
        "        ============================",
        extra={"interview_id": interview_id},
    )
