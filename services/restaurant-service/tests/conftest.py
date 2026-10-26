"""Fixtures for the `db`-marked tests: a separate `restaurant_db_test`
database on the compose restaurant-db (published on localhost:15433),
migrated with the real Alembic migrations, emptied before each test. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sfo_common.testing import prepare_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "RESTAURANT_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15433/restaurant_db_test",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables, the activities bound to the test database, and cache
    invalidations recorded instead of sent to Redis."""
    from app import activities, cache

    # Pool big enough for the concurrency tests' simultaneous transactions.
    engine = create_async_engine(migrated_database, pool_size=25, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE restaurants, menu_categories, menu_items, promotions,"
                " stock_reservations, promotion_redemptions, restaurant_status_history,"
                " outbox_events CASCADE"
            )
        )
    monkeypatch.setattr(activities, "SessionFactory", factory)

    invalidated: list[str] = []

    async def _invalidate(restaurant_id: str) -> None:
        invalidated.append(restaurant_id)

    monkeypatch.setattr(cache, "invalidate", _invalidate)
    yield factory
    await engine.dispose()
