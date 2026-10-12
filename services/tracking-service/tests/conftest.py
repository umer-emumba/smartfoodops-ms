"""tracking-service tests run against Redis DB 15 on the compose Redis
(localhost:16379, `db` marker — scripts/check.sh --with-db) and a real local
HTTP server, so streaming behaves as it does in production."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis, from_url
from sfo_auth import Role, create_access_token
from sfo_common.testing import serve

TEST_REDIS_URL = os.environ.get("TRACKING_TEST_REDIS_URL", "redis://localhost:16379/15")
# Tests mint their own tokens, so any secret works when there's no .env.
os.environ.setdefault("JWT_SECRET", "tracking-service-test-secret-32-bytes-long")


@dataclass
class Env:
    redis: Redis
    base_url: str
    hub: Any
    # Fallback calls to order-/restaurant-service, keyed by URL suffix.
    fallback: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    fallback_calls: list[str] = field(default_factory=list)


def token(user_id: str, role: Role) -> str:
    from app.config import settings

    return create_access_token(
        user_id=user_id,
        role=role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=5,
    )


def auth(user_id: str, role: Role = Role.CUSTOMER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, role)}"}


@pytest_asyncio.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    from app import redis_client, state
    from app.api import routes
    from app.config import settings
    from app.hub import Hub
    from app.main import app

    redis = from_url(TEST_REDIS_URL, decode_responses=True)
    await redis.flushdb()
    hub = Hub(redis, queue_size=settings.client_queue_size)
    monkeypatch.setattr(redis_client, "redis", redis)
    monkeypatch.setattr(routes, "hub", hub)
    monkeypatch.setattr(settings, "heartbeat_seconds", 0.3)

    async with serve(app) as base_url:
        test_env = Env(redis=redis, base_url=base_url, hub=hub)

        async def _fetch(url: str) -> dict[str, Any] | None:
            suffix = url.split("/internal/", 1)[1]
            test_env.fallback_calls.append(suffix)
            return test_env.fallback.get(suffix)

        monkeypatch.setattr(state, "_fetch", _fetch)
        yield test_env
    await hub.close()
    await redis.aclose()
