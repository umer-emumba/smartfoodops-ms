"""Fixtures for the `db`-marked tests: a separate `order_db_test` database on
the compose order-db (published on localhost:15434), migrated with the real
Alembic migrations, emptied before each test. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from sfo_common.testing import prepare_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "ORDER_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15434/order_db_test",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables, and the activities and API bound to the test database."""
    from app import activities, reconciler
    from app.db import get_session
    from app.main import app

    engine = create_async_engine(migrated_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE orders, order_items, order_status_history, outbox_events CASCADE")
        )
    monkeypatch.setattr(activities, "SessionFactory", factory)
    monkeypatch.setattr(reconciler, "SessionFactory", factory)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    yield factory
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


class RecordingRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """Live-tracking publishes, captured instead of sent to Redis."""
    from app import tracking

    fake = RecordingRedis()
    monkeypatch.setattr(tracking, "redis", fake)
    yield fake.published


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    """Signals the API sends to the order saga, captured instead of sent."""
    from app import temporal_client

    sent: list[tuple[str, str, Any]] = []

    async def _signal(order_id: str, signal: str, payload: Any) -> None:
        sent.append((order_id, signal, payload))

    monkeypatch.setattr(temporal_client, "signal_order_saga", _signal)
    return sent
