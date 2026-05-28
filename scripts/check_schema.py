import asyncio, os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def check():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'candidates' "
            "ORDER BY ordinal_position"
        ))
        print('== candidates ==')
        for row in r:
            print(f'  {row[0]:30} {row[1]}')

        r2 = await conn.execute(text(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'interview_extracted_data' "
            "ORDER BY ordinal_position"
        ))
        print('== interview_extracted_data ==')
        for row in r2:
            print(f'  {row[0]:30} {row[1]}')

    await engine.dispose()

asyncio.run(check())
