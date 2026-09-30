from sfo_common.db import make_engine, make_session_factory
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """payment_db's own declarative base — not shared with any other service."""


engine = make_engine(settings.database_url)
SessionFactory = make_session_factory(engine)


async def get_session():
    async with SessionFactory() as session:
        yield session
