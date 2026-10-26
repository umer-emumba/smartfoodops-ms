"""Fixtures for the `db`-marked tests: a separate `payment_db_test` database
on the compose payment-db (published on localhost:15436), migrated with the
real Alembic migrations, emptied before each test. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sfo_common.testing import prepare_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "PAYMENT_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15436/payment_db_test",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables, and the activities bound to the test database."""
    from app import activities

    engine = create_async_engine(migrated_database, pool_size=15, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, refunds, outbox_events CASCADE"))
    monkeypatch.setattr(activities, "SessionFactory", factory)
    yield factory
    await engine.dispose()
