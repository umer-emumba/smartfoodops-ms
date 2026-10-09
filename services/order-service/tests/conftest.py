"""Fixtures for the `db`-marked tests: a separate `order_db_test` database on
the compose order-db (published on localhost:15434), migrated with the real
Alembic migrations, emptied before each test. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "ORDER_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15434/order_db_test",
)


async def _create_database_if_missing(url: str) -> None:
    base, name = url.replace("postgresql+asyncpg", "postgresql").rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))
    subprocess.run(
        ["alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        check=True,
        capture_output=True,
    )
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables, and the activities and API bound to the test database."""
    from app import activities
    from app.db import get_session
    from app.main import app

    engine = create_async_engine(migrated_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE orders, order_items, order_status_history, outbox_events CASCADE")
        )
    monkeypatch.setattr(activities, "SessionFactory", factory)

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
