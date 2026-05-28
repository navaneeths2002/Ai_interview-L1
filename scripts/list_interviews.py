import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT i.id, i.status, i.created_at, i.duration_seconds,
                   (c.first_name || ' ' || COALESCE(c.last_name, '')) AS candidate_name,
                   (SELECT COUNT(*) FROM interview_transcripts t WHERE t.interview_id = i.id) AS turns,
                   (SELECT COUNT(*) FROM interview_scores   s  WHERE s.interview_id  = i.id::text) AS has_score,
                   (SELECT COUNT(*) FROM interview_reports  rp WHERE rp.interview_id = i.id::text) AS has_report
            FROM interviews i
            LEFT JOIN candidates c ON c.id = i.candidate_id
            ORDER BY i.created_at DESC
            LIMIT 10
        """))
        rows = r.fetchall()
        if not rows:
            print("No interviews found.")
            return
        print(f"{'#':<3}  {'ID':<38}  {'Candidate':<22}  {'Status':<12}  {'Turns':>5}  Score  Report  Duration")
        print("-" * 110)
        for n, row in enumerate(rows, 1):
            iid, status, created, duration, cname, turns, has_score, has_report = row
            score_flag  = "YES" if has_score  else "NO "
            report_flag = "YES" if has_report else "NO "
            dur = f"{duration}s" if duration else "--"
            print(f"{n:<3}  {str(iid):<38}  {(cname or 'unknown'):<22}  {status:<12}  {turns:>5}  {score_flag}    {report_flag}     {dur}")
    await engine.dispose()

asyncio.run(main())
