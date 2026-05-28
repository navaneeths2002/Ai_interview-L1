"""
Backfill script — JSONB migration
===================================
Migrates existing rows that were created BEFORE the add_profile_extracted_jsonb
Alembic migration. Runs once; safe to re-run (idempotent).

  python scripts/backfill_profile_extracted_jsonb.py

What it does:
  For every `interview_extracted_data` row where `extracted IS NULL`
  but `raw_extraction IS NOT NULL`:
    - Pulls the "extracted" key out of raw_extraction (full Claude output)
    - Writes it to the `extracted` JSONB column

Note: candidates.profile backfill is not needed — new triggers write it directly.
Any candidates created before this migration will have profile=NULL and will
receive their profile data on the next interview trigger for that candidate.
"""

import asyncio
import json
import os
import sys

# Make sure we can import app modules from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


async def backfill(session: AsyncSession) -> None:
    # ── Backfill interview_extracted_data.extracted from raw_extraction ────
    ext_rows = (await session.execute(text("""
        SELECT id, raw_extraction
        FROM interview_extracted_data
        WHERE extracted IS NULL
          AND raw_extraction IS NOT NULL
    """))).fetchall()

    print(f"[backfill] {len(ext_rows)} interview_extracted_data rows to backfill…")
    for row in ext_rows:
        raw = row.raw_extraction or {}
        extracted_doc = raw.get("extracted", {})
        if extracted_doc:
            await session.execute(
                text("UPDATE interview_extracted_data SET extracted = :doc WHERE id = :id"),
                {"doc": json.dumps(extracted_doc), "id": str(row.id)},
            )

    print(f"[backfill] {len(ext_rows)} extracted docs written OK")

    await session.commit()
    print("[backfill] all done OK")


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL not set in .env")
        sys.exit(1)

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await backfill(session)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
