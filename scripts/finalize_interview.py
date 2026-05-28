"""
finalize_interview.py
======================
Force-completes a stuck interview, then runs evaluation + report in sequence.
Use when the agent process was killed before on_shutdown could run.

Usage:
    python scripts/finalize_interview.py <interview_id>
"""

import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(override=True)   # override=True so keys in .env win over stale env vars

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.models.interview import Interview


async def finalize(interview_id: str) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # ── Step 1: force status → completed ──────────────────────────────────────
    async with factory() as session:
        interview = (await session.execute(
            select(Interview).where(Interview.id == interview_id)
        )).scalar_one_or_none()

        if not interview:
            print(f"ERROR: interview {interview_id} not found")
            sys.exit(1)

        tenant_id = interview.tenant_id   # save before session closes
        print(f"Interview : {interview_id}")
        print(f"Candidate : {interview.candidate_id}")
        print(f"Tenant    : {tenant_id}")
        print(f"Status    : {interview.status}")

        if interview.status == "completed":
            print("Already completed — skipping status update")
        else:
            now = datetime.now(timezone.utc)
            # estimate duration from transcript timestamps if available
            row = (await session.execute(text("""
                SELECT EXTRACT(EPOCH FROM (MAX(spoken_at) - MIN(spoken_at)))::int
                FROM interview_transcripts WHERE interview_id = :iid
            """), {"iid": interview_id})).scalar_one_or_none()

            duration = row or 0
            await session.execute(text("""
                UPDATE interviews
                SET status = 'completed',
                    ended_at = :now,
                    duration_seconds = :dur,
                    updated_at = :now
                WHERE id = :iid
            """), {"now": now, "dur": duration, "iid": interview_id})
            await session.commit()
            print(f"Status    -> completed  (duration ~{duration}s)")

    # ── Step 2: run evaluation (Claude Haiku scoring) ─────────────────────────
    print("\n--- Running evaluation ---")
    from app.services.evaluation_engine import run_evaluation
    await run_evaluation(interview_id)
    print("Evaluation complete")

    # ── Step 3: run report generation ─────────────────────────────────────────
    print("\n--- Generating HTML report ---")
    from app.services.report_generator import run_report
    await run_report(interview_id)
    print("Report complete")

    await engine.dispose()

    print()
    print("=" * 60)
    print("  DONE! Access your results:")
    print("=" * 60)
    print(f"  HTML Report (paste in browser):")
    print(f"  http://localhost:8000/api/v1/interviews/{interview_id}/report/html")
    print()
    print(f"  JSON Scores (Postman GET):")
    print(f"  /api/v1/interviews/{interview_id}/evaluation")
    print(f"  Header: X-Tenant-ID: {tenant_id}")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Auto-pick the most recent in_progress interview with turns
        async def auto_pick():
            engine = create_async_engine(os.environ["DATABASE_URL"])
            async with engine.connect() as conn:
                r = await conn.execute(text("""
                    SELECT i.id FROM interviews i
                    WHERE i.status = 'in_progress'
                      AND (SELECT COUNT(*) FROM interview_transcripts t WHERE t.interview_id = i.id) > 0
                    ORDER BY i.created_at DESC LIMIT 1
                """))
                row = r.fetchone()
            await engine.dispose()
            return str(row[0]) if row else None

        iid = asyncio.run(auto_pick())
        if not iid:
            print("Usage: python scripts/finalize_interview.py <interview_id>")
            print("No in_progress interview with transcripts found automatically.")
            sys.exit(1)
        print(f"Auto-selected interview: {iid}\n")
    else:
        iid = sys.argv[1]

    asyncio.run(finalize(iid))
