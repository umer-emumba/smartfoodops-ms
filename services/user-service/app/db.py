from collections.abc import AsyncIterator

from sfo_common.db import make_engine, make_session_factory
from sfo_observability import instrument_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """user_db's own declarative base — not shared with any other service."""


engine = make_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout_seconds=settings.db_pool_timeout_seconds,
    statement_timeout_ms=settings.db_statement_timeout_ms,
)
SessionFactory = make_session_factory(engine)
instrument_engine(engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
