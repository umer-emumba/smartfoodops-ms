"""Helpers for services' `db`-marked tests (scripts/check.sh --with-db):
a separate `*_test` database on the service's compose Postgres, migrated
with the service's real Alembic migrations. Imported only from tests."""

import asyncio
import os
import subprocess


async def _create_database_if_missing(url: str) -> None:
    import asyncpg  # every service with a database has it

    base, name = url.replace("postgresql+asyncpg", "postgresql").rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def prepare_test_database(url: str) -> str:
    """Creates the database if needed and runs `alembic upgrade head` against
    it (from the service directory, where tests run). Returns `url`."""
    asyncio.run(_create_database_if_missing(url))
    subprocess.run(
        ["alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url
