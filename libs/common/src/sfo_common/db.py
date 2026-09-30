from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Every service defines its own models against its own Base in practice,
    but services without extra needs can just import this one."""


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def session_dependency(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async def _get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    return _get_session  # type: ignore[return-value]


async def check_db_ready(engine: AsyncEngine) -> bool:
    """Used as the readiness check behind GET /health."""
    try:
        async with engine.connect() as conn:
            await conn.run_sync(lambda _: None)
        return True
    except Exception:
        return False
