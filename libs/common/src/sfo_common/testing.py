"""Helpers for services' `db`-marked tests (scripts/check.sh --with-db):
a separate `*_test` database on the service's compose Postgres, migrated
with the service's real Alembic migrations — and `serve`, a real HTTP server
for streaming tests. Imported only from tests."""

import asyncio
import contextlib
import os
import subprocess
from collections.abc import AsyncGenerator
from typing import Any


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


@contextlib.asynccontextmanager
async def serve(app: Any) -> AsyncGenerator[str, None]:
    """Runs an ASGI app on a real local port for the duration of the block
    and yields its base URL — for tests that need real streaming (httpx's
    ASGITransport buffers the whole response)."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surface a startup failure
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        # Open streams would otherwise hold a graceful shutdown forever.
        server.force_exit = True
        server.should_exit = True
        await task
