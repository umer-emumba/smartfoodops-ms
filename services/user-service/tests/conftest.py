"""Fixtures for the `db`-marked tests: a separate `user_db_test` database on
the compose user-db (localhost:15432), migrated with the real Alembic
migrations and emptied before each test. Run with `scripts/check.sh
--with-db` (or `pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sfo_common.testing import prepare_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "USER_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15432/user_db_test",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_database)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, refresh_tokens, addresses CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
