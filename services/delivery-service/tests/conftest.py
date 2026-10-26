"""Fixtures for the `db`-marked tests: a separate `delivery_db_test` database
on the compose delivery-db (localhost:15435), migrated with the real Alembic
migrations, and Redis DB 15 on the compose Redis (localhost:16379) — both
emptied before each test. Run with `scripts/check.sh --with-db` (or
`pytest -m db`) while `docker compose up`."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis, from_url
from sfo_common.testing import prepare_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "DELIVERY_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15435/delivery_db_test",
)
TEST_REDIS_URL = os.environ.get("DELIVERY_TEST_REDIS_URL", "redis://localhost:16379/15")


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@dataclass
class Env:
    factory: async_sessionmaker[AsyncSession]
    redis: Redis
    # (order_id, message) for every tracking publish.
    published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # (order_id, signal, args) sent to the order saga.
    signals: list[tuple[str, str, tuple[Any, ...]]] = field(default_factory=list)


@pytest_asyncio.fixture
async def env(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    from app import activities, redis_client, stale_riders, temporal_client, tracking
    from app.api import routes
    from app.db import get_session
    from app.main import app

    engine = create_async_engine(migrated_database, pool_size=15, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE riders, deliveries, delivery_offers, delivery_status_history,"
                " rider_verification_history, outbox_events CASCADE"
            )
        )
    redis = from_url(TEST_REDIS_URL, decode_responses=True)
    await redis.flushdb()

    test_env = Env(factory=factory, redis=redis)
    monkeypatch.setattr(activities, "SessionFactory", factory)
    monkeypatch.setattr(stale_riders, "SessionFactory", factory)
    monkeypatch.setattr(redis_client, "redis", redis)

    async def _publish(
        order_id: str, message: dict[str, Any], state: dict[str, Any] | None = None
    ) -> None:
        test_env.published.append((order_id, message))

    async def _signal(order_id: str, signal: str, *args: Any) -> None:
        test_env.signals.append((order_id, signal, args))

    async def _no_mongo(*_: Any) -> None:
        return None

    monkeypatch.setattr(tracking, "_publish", _publish)
    monkeypatch.setattr(temporal_client, "signal_order_saga", _signal)
    monkeypatch.setattr(routes, "insert_location_ping", _no_mongo)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    yield test_env
    app.dependency_overrides.pop(get_session, None)
    await redis.aclose()
    await engine.dispose()
