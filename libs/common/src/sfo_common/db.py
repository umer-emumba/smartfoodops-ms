from collections.abc import AsyncGenerator

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Every service defines its own models against its own Base in practice,
    but services without extra needs can just import this one."""


def make_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_timeout_seconds: float = 3.0,
    statement_timeout_ms: int = 5000,
    idle_in_transaction_timeout_ms: int = 15000,
) -> AsyncEngine:
    """Pool limits are explicit because they multiply: every replica opens up
    to pool_size + max_overflow connections against the same Postgres.

    pool_timeout is short so a saturated pool fails fast (503, see
    sfo_common.errors) instead of queueing requests for SQLAlchemy's 30s
    default. The two server-side timeouts stop one slow query, or a request
    that holds a transaction open while it waits on something else, from
    pinning a connection indefinitely.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_seconds,
        connect_args={
            "server_settings": {
                "statement_timeout": str(statement_timeout_ms),
                "idle_in_transaction_session_timeout": str(idle_in_transaction_timeout_ms),
            }
        },
    )


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


# Arbitrary fixed key — every service has its own database, so the same value
# never collides across services.
_MIGRATION_LOCK_KEY = 727_001


def acquire_migration_lock(connection: Connection) -> None:
    """Called from each service's alembic/env.py inside the migration
    transaction. Every replica runs `alembic upgrade head` on start; the first
    one takes this lock and migrates, the rest block here until it commits,
    then find nothing left to do. Transaction-scoped, so it's released on
    commit or rollback, never leaked."""
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
