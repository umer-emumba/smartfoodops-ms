"""Public-read cache (app/cache.py): what gets cached, who it's served to,
and that writes invalidate it."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app import cache, repository
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import MenuItem, Restaurant
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sfo_auth import Role, create_access_token

RID = "0198abc1-2345-7def-8901-abcdef123456"
OWNER = "owner-1"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class _FakeSession:
    def add(self, _obj) -> None:
        pass

    async def commit(self) -> None:
        pass


def _token(user_id: str, role: Role) -> dict[str, str]:
    token = create_access_token(
        user_id=user_id,
        role=role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=15,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(cache, "redis", fake_redis)
    restaurant = Restaurant(
        id=RID,
        admin_user_id=OWNER,
        name="Test Kitchen",
        address="1 Test St",
        city="Karachi",
        lat=24.86,
        lng=67.01,
        contact_phone="+920000000",
        cuisine_type=None,
        status="active",
        is_accepting_orders=True,
        max_concurrent_orders=20,
        avg_prep_time_minutes=20,
        # Column defaults only apply on flush; these objects never touch a DB.
        created_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    calls = {"menu": 0}

    async def get_restaurant(_session, _id):
        return restaurant

    async def list_menu(_session, _id):
        calls["menu"] += 1
        return [
            MenuItem(
                id="m1",
                restaurant_id=RID,
                category_id="c1",
                name="Biryani",
                description="",
                price=Decimal("8.50"),
                is_available=True,
                stock_count=None,
                version=1,
            )
        ]

    monkeypatch.setattr(repository, "get_restaurant", get_restaurant)
    monkeypatch.setattr(repository, "list_menu", list_menu)
    app.dependency_overrides[get_session] = lambda: _FakeSession()
    yield {"redis": fake_redis, "restaurant": restaurant, "calls": calls}
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_active_menu_cached_and_publicly_cacheable(env) -> None:
    async with _client() as c:
        first = await c.get(f"/v1/restaurants/{RID}/menu")
        second = await c.get(f"/v1/restaurants/{RID}/menu")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()[0]["name"] == "Biryani"
    assert env["calls"]["menu"] == 1  # second request never reached the DB
    assert second.headers["cache-control"] == cache.PUBLIC_CACHE_CONTROL


@pytest.mark.asyncio
async def test_suspended_restaurant_never_cached_or_shared(env) -> None:
    env["restaurant"].status = "suspended"
    async with _client() as c:
        anon = await c.get(f"/v1/restaurants/{RID}/menu")
        owner = await c.get(
            f"/v1/restaurants/{RID}/menu", headers=_token(OWNER, Role.RESTAURANT_ADMIN)
        )
    assert anon.status_code == 404
    assert owner.status_code == 200
    assert owner.headers["cache-control"] == cache.PRIVATE_CACHE_CONTROL
    assert env["redis"].store == {}


@pytest.mark.asyncio
async def test_status_change_invalidates_cached_views(env) -> None:
    async with _client() as c:
        await c.get(f"/v1/restaurants/{RID}")
        await c.get(f"/v1/restaurants/{RID}/menu")
        assert len(env["redis"].store) == 2
        r = await c.post(
            f"/v1/restaurants/{RID}/suspend", headers=_token("admin-1", Role.SYSTEM_ADMIN)
        )
        assert r.status_code == 200
        assert env["redis"].store == {}
        # Now suspended: the public no longer sees it, cache or not.
        assert (await c.get(f"/v1/restaurants/{RID}/menu")).status_code == 404


@pytest.mark.asyncio
async def test_redis_down_falls_back_to_database(env, monkeypatch) -> None:
    async def down(*_a, **_k):
        raise RedisConnectionError("down")

    monkeypatch.setattr(env["redis"], "get", down)
    monkeypatch.setattr(env["redis"], "set", down)
    async with _client() as c:
        r = await c.get(f"/v1/restaurants/{RID}/menu")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_public_list_is_cacheable(env, monkeypatch) -> None:
    async def list_restaurants(_session, **_kw):
        return {"items": [env["restaurant"]], "next_cursor": None}

    monkeypatch.setattr(repository, "list_restaurants", list_restaurants)
    async with _client() as c:
        r = await c.get("/v1/restaurants")
    assert r.status_code == 200
    assert r.headers["cache-control"] == cache.PUBLIC_CACHE_CONTROL
    assert r.json()["items"][0]["id"] == RID
