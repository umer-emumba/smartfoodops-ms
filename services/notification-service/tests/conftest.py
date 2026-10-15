"""Fixtures for the `db`-marked tests: a separate `notification_db_test`
database on the compose notification-db (published on localhost:15437),
migrated with the real Alembic migrations, emptied before each test. Run
with `scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose
up`."""

import os

# The Celery app refuses to load without a broker; tests never reach one
# (enqueueing is replaced by `enqueued` below).
os.environ.setdefault("CELERY_BROKER_URL", "memory://")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sfo_common.testing import prepare_test_database  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DATABASE_URL = os.environ.get(
    "NOTIFICATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:15437/notification_db_test",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    return prepare_test_database(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables, and the tasks and API bound to the test database."""
    from app import tasks
    from app.db import get_session
    from app.main import app

    engine = create_async_engine(migrated_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE notifications, processed_events"))
    monkeypatch.setattr(tasks, "SessionFactory", factory)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    yield factory
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Notification IDs the consumer enqueued for sending."""
    from app import celery_app

    sent: list[str] = []
    monkeypatch.setattr(celery_app, "enqueue_sends", sent.extend)
    return sent
