from sfo_common.db import make_engine, make_session_factory
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """restaurant_db's own declarative base — not shared with any other service."""


engine = make_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout_seconds=settings.db_pool_timeout_seconds,
    statement_timeout_ms=settings.db_statement_timeout_ms,
)
SessionFactory = make_session_factory(engine)


async def get_session():
    async with SessionFactory() as session:
        yield session
