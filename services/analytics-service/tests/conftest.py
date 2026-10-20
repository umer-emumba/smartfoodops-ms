"""Fixtures for the `db`-marked tests: a separate `analytics_test` database
on the compose Mongo (published on localhost:27018), dropped before each
test. Run with `scripts/check.sh --with-db` (or `pytest -m db`) while
`docker compose up`."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

TEST_MONGO_URL = os.environ.get("ANALYTICS_TEST_MONGO_URL", "mongodb://localhost:27018")


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncDatabase]:
    """An empty, indexed analytics database, also used by the API."""
    from app import mongo

    client: AsyncMongoClient = AsyncMongoClient(
        TEST_MONGO_URL, serverSelectionTimeoutMS=2000, tz_aware=True
    )
    await client.drop_database("analytics_test")
    database = client["analytics_test"]
    await mongo.ensure_indexes(database)
    monkeypatch.setattr(mongo, "db", database)
    yield database
    await client.close()
