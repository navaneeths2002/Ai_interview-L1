from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


# NullPool — no connection is cached/reused between checkouts. This is REQUIRED
# because the LiveKit agent worker runs each interview job on its OWN event loop:
# a pooled asyncpg connection created on job #1's loop, reused on job #2's loop,
# raises "cannot perform operation: another operation is in progress" /
# "Future attached to a different loop". NullPool opens a fresh connection per
# session on the current loop and closes it on release — loop-safe everywhere.
# Overhead is negligible at this service's scale (a few ms per connection).
engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "development",
    poolclass=NullPool,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
