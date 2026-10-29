"""Fixtures for the `db`-marked tests: a separate `order_db_test` database on
the compose order-db (published on localhost:15434), migrated with the real
Alembic migrations, emptied before each test. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator, Callable
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
    """Stands in for Redis in app/tracking.py: applies pipelined state writes
    to dicts and records publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> "RecordingPipeline":
        return RecordingPipeline(self)


class RecordingPipeline:
    def __init__(self, redis: RecordingRedis) -> None:
        self._redis = redis
        self._ops: list[Callable[[], None]] = []

    async def __aenter__(self) -> "RecordingPipeline":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self._ops.append(
            lambda: self._redis.hashes.setdefault(key, {}).update(
                {k: str(v) for k, v in mapping.items()}
            )
        )

    def expire(self, key: str, seconds: int) -> None:
        self._ops.append(lambda: self._redis.ttls.__setitem__(key, seconds))

    def sadd(self, key: str, member: str) -> None:
        self._ops.append(lambda: self._redis.sets.setdefault(key, set()).add(member))

    def srem(self, key: str, member: str) -> None:
        self._ops.append(lambda: self._redis.sets.setdefault(key, set()).discard(member))

    def publish(self, channel: str, message: str) -> None:
        self._ops.append(lambda: self._redis.published.append((channel, message)))

    async def execute(self) -> list[Any]:
        for op in self._ops:
            op()
        return []


@pytest.fixture(autouse=True)
def tracking_redis(monkeypatch: pytest.MonkeyPatch) -> RecordingRedis:
    """Every test gets the recording Redis — none touches a real one."""
    from app import redis_client

    fake = RecordingRedis()
    monkeypatch.setattr(redis_client, "redis", fake)
    return fake


@pytest.fixture
def published(tracking_redis: RecordingRedis) -> list[tuple[str, str]]:
    """Live-tracking publishes as (channel, JSON message)."""
    return tracking_redis.published


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    """Signals the API sends to the order saga, captured instead of sent."""
    from app import temporal_client

    sent: list[tuple[str, str, Any]] = []

    async def _signal(order_id: str, signal: str, *payload: Any) -> None:
        sent.append((order_id, signal, payload[0] if payload else None))

    monkeypatch.setattr(temporal_client, "signal_order_saga", _signal)
    return sent
