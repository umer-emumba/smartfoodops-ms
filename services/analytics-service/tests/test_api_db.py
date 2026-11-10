"""The analytics API: who may see what, and the time window."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app import facts, owners
from app.config import settings
from app.main import app
from httpx import ASGITransport, AsyncClient
from pymongo.asynchronous.database import AsyncDatabase
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sfo_events import EventType

from tests.events import order

pytestmark = pytest.mark.db

WINDOW = {"from": "2026-10-15T06:00:00Z", "to": "2026-10-15T10:00:00Z"}


def _as(user_id: str, role: Role) -> dict[str, str]:
    token = make_test_token(
        user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url, ttl_minutes=5
    )
    return {"Authorization": f"Bearer {token}"}


ADMIN = _as("u-admin", Role.SYSTEM_ADMIN)
OWNER = _as("u-owner", Role.RESTAURANT_ADMIN)
OTHER_OWNER = _as("u-other", Role.RESTAURANT_ADMIN)
CUSTOMER = _as("u-customer", Role.CUSTOMER)


# restaurant-service's view: r-1 and r-new are u-owner's; anything else
# doesn't exist.
RESTAURANT_OWNERS = {"r-1": "u-owner", "r-new": "u-owner"}


@pytest_asyncio.fixture
async def client(db: AsyncDatabase, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async def restaurant_owner(restaurant_id: str) -> str | None:
        return RESTAURANT_OWNERS.get(restaurant_id)

    monkeypatch.setattr(owners, "restaurant_owner", restaurant_owner)
    for ev in (order(EventType.ORDER_PLACED, 0), order(EventType.ORDER_CONFIRMED, 2)):
        await facts.apply_event(db, ev)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_admin_summary(client: AsyncClient) -> None:
    resp = await client.get("/v1/analytics/summary", params=WINDOW, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["orders"]["total_orders"] == 1
    assert body["orders"]["restaurant_acceptance_rate"] == 1.0
    assert body["orders_per_restaurant"] == [{"restaurant_id": "r-1", "orders": 1}]
    assert body["failed_events"] == {"dead_lettered": 0, "saga_system_errors": 0, "total": 0}
    assert len(body["peak_hours"]) == 24


async def test_summary_and_peak_hours_are_admin_only(client: AsyncClient) -> None:
    for path in ("/v1/analytics/summary", "/v1/analytics/peak-hours"):
        assert (await client.get(path, headers=OWNER)).status_code == 403
        assert (await client.get(path, headers=CUSTOMER)).status_code == 403
        assert (await client.get(path)).status_code == 401


async def test_restaurant_admin_sees_only_their_restaurant(client: AsyncClient) -> None:
    mine = await client.get("/v1/analytics/restaurants/r-1", params=WINDOW, headers=OWNER)
    assert mine.status_code == 200 and mine.json()["orders"]["total_orders"] == 1
    other = await client.get("/v1/analytics/restaurants/r-1", params=WINDOW, headers=OTHER_OWNER)
    assert other.status_code == 403
    admin = await client.get("/v1/analytics/restaurants/r-1", params=WINDOW, headers=ADMIN)
    assert admin.status_code == 200


async def test_new_restaurant_admin_gets_empty_stats(client: AsyncClient) -> None:
    # No orders yet, so ownership comes from restaurant-service.
    mine = await client.get("/v1/analytics/restaurants/r-new", params=WINDOW, headers=OWNER)
    assert mine.status_code == 200
    assert mine.json()["orders"]["total_orders"] == 0 and len(mine.json()["peak_hours"]) == 24
    other = await client.get("/v1/analytics/restaurants/r-new", params=WINDOW, headers=OTHER_OWNER)
    assert other.status_code == 403


async def test_unknown_restaurant_is_not_found(client: AsyncClient) -> None:
    resp = await client.get("/v1/analytics/restaurants/r-nope", params=WINDOW, headers=OWNER)
    assert resp.status_code == 404


async def test_window_must_run_forwards(client: AsyncClient) -> None:
    resp = await client.get(
        "/v1/analytics/peak-hours",
        params={"from": WINDOW["to"], "to": WINDOW["from"]},
        headers=ADMIN,
    )
    assert resp.status_code == 422


async def test_default_window_is_the_last_week(client: AsyncClient) -> None:
    body = (await client.get("/v1/analytics/peak-hours", headers=ADMIN)).json()
    span = body["window"]
    assert span["start"] < span["end"] and body["timezone"] == settings.analytics_timezone
