"""Rider endpoints, location ingestion and delivery lookup (week2-plan
step 5) against a real Postgres and Redis; signals to the saga captured."""

from datetime import UTC, datetime, timedelta

import pytest
from app import activities, rider_locations
from app.config import settings
from app.main import app
from app.models import Delivery, Rider
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token
from sfo_contracts import OrderSagaSignal
from sfo_contracts.activities import TransitionDeliveryInput
from sfo_contracts.workflows import RiderDropped, RiderResponse

from tests.conftest import Env
from tests.helpers import CUSTOMER, add_rider, assigned, km_north, offered, outbox_payloads

pytestmark = pytest.mark.db


def _as(user_id: str, role: Role = Role.RIDER) -> dict[str, str]:
    token = create_access_token(
        user_id=user_id,
        role=role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=5,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _rider(env: Env, rider_id: str) -> Rider:
    async with env.factory() as session:
        rider = await session.get(Rider, rider_id)
        assert rider is not None
        return rider


# ---------- Location ingestion ----------


async def test_location_feeds_the_pool_and_throttles_postgres(
    env: Env, client: AsyncClient
) -> None:
    await add_rider(env, "r1")  # available, no position yet
    me = _as("user-r1")
    resp = await client.patch(
        "/v1/riders/me/location", json={"lat": 24.82, "lng": 67.03}, headers=me
    )
    assert resp.status_code == 200
    assert await env.redis.zscore(rider_locations.AVAILABLE_KEY, "r1") is not None
    first = await _rider(env, "r1")
    assert (first.last_lat, first.last_location_at is not None) == (24.82, True)

    # Within 30s: Redis moves, Postgres doesn't.
    await client.patch("/v1/riders/me/location", json={"lat": 24.83, "lng": 67.03}, headers=me)
    assert await rider_locations.latest("r1") == (24.83, 67.03)
    assert (await _rider(env, "r1")).last_lat == 24.82


async def test_held_rider_stays_out_of_the_pool_and_is_tracked(
    env: Env, client: AsyncClient
) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    await assigned(env, "o-1")
    env.published.clear()
    resp = await client.patch(
        "/v1/riders/me/location", json={"lat": 24.818, "lng": 67.03}, headers=_as("user-r1")
    )
    assert resp.status_code == 200
    assert await env.redis.zscore(rider_locations.AVAILABLE_KEY, "r1") is None
    assert env.published == [
        ("o-1", {"type": "location", "rider_id": "r1", "lat": 24.818, "lng": 67.03})
    ]


async def test_offered_rider_location_is_not_published_to_the_customer(
    env: Env, client: AsyncClient
) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    await offered(env)
    env.published.clear()
    await client.patch(
        "/v1/riders/me/location", json={"lat": 24.818, "lng": 67.03}, headers=_as("user-r1")
    )
    assert env.published == []


async def test_status_change_updates_the_pool_but_not_mid_delivery(
    env: Env, client: AsyncClient
) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    me = _as("user-r1")
    await client.patch("/v1/riders/me/status", json={"status": "offline"}, headers=me)
    assert await env.redis.zscore(rider_locations.AVAILABLE_KEY, "r1") is None
    await client.patch("/v1/riders/me/status", json={"status": "available"}, headers=me)
    assert await env.redis.zscore(rider_locations.AVAILABLE_KEY, "r1") is not None

    await offered(env)
    resp = await client.patch("/v1/riders/me/status", json={"status": "offline"}, headers=me)
    assert resp.status_code == 409
    # Off and back on shift; the offer's available → busy isn't a shift change.
    shifts = await outbox_payloads(env, "RiderShiftChanged")
    assert [(s["to_status"], s["on_shift"], s["reason"]) for s in shifts] == [
        ("offline", False, "rider"),
        ("available", True, "rider"),
    ]


async def test_suspension_ends_the_shift(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    resp = await client.post("/v1/riders/r1/suspend", headers=_as("admin", Role.SYSTEM_ADMIN))
    assert resp.status_code == 200
    [shift] = await outbox_payloads(env, "RiderShiftChanged")
    assert (shift["from_status"], shift["on_shift"], shift["reason"]) == (
        "available",
        False,
        "suspended",
    )


# ---------- Offer, accept, decline ----------


async def test_rider_sees_and_accepts_their_offer(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, offer = await offered(env)
    me = _as("user-r1")

    view = await client.get("/v1/riders/me/offer", headers=me)
    assert view.status_code == 200
    assert view.json()["delivery_id"] == delivery_id
    assert view.json()["distance_to_pickup_km"] == pytest.approx(0.5, abs=0.01)

    resp = await client.post(f"/v1/riders/me/deliveries/{delivery_id}/accept", headers=me)
    assert resp.status_code == 202
    assert env.signals == [
        (
            "o-1",
            OrderSagaSignal.RIDER_RESPONSE,
            (RiderResponse(delivery_id=delivery_id, rider_id="r1", accepted=True),),
        )
    ]


async def test_decline_signals_not_accepted(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await offered(env)
    resp = await client.post(
        f"/v1/riders/me/deliveries/{delivery_id}/decline", headers=_as("user-r1")
    )
    assert resp.status_code == 202
    assert env.signals[0][2] == (
        RiderResponse(delivery_id=delivery_id, rider_id="r1", accepted=False),
    )


async def test_no_offer_is_404(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    assert (await client.get("/v1/riders/me/offer", headers=_as("user-r1"))).status_code == 404


async def test_rider_cannot_accept_another_riders_offer(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    await add_rider(env, "r2", at=km_north(3))
    delivery_id, offer = await offered(env)
    assert offer.rider_id == "r1"
    resp = await client.post(
        f"/v1/riders/me/deliveries/{delivery_id}/accept", headers=_as("user-r2")
    )
    assert resp.status_code == 403
    assert env.signals == []


async def test_expired_offer_cannot_be_accepted(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await offered(env)
    async with env.factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        assert delivery is not None
        delivery.offer_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    resp = await client.post(
        f"/v1/riders/me/deliveries/{delivery_id}/accept", headers=_as("user-r1")
    )
    assert resp.status_code == 409
    assert env.signals == []


# ---------- Drop-out, pickup, delivered ----------


async def test_drop_out_before_pickup(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    resp = await client.post(
        f"/v1/riders/me/deliveries/{delivery_id}/drop-out",
        json={"reason": "Flat tyre"},
        headers=_as("user-r1"),
    )
    assert resp.status_code == 202
    assert env.signals[0][1:] == (
        OrderSagaSignal.RIDER_DROPPED,
        (RiderDropped(delivery_id=delivery_id, rider_id="r1", reason="Flat tyre"),),
    )


async def test_no_drop_out_after_pickup(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await activities.transition_delivery(
        TransitionDeliveryInput(delivery_id=delivery_id, to_status="picked_up")
    )
    resp = await client.post(
        f"/v1/riders/me/deliveries/{delivery_id}/drop-out",
        json={"reason": "Tired"},
        headers=_as("user-r1"),
    )
    assert resp.status_code == 409
    assert env.signals == []


async def test_pickup_then_delivered_signal_in_order(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    me = _as("user-r1")
    # Not picked up yet → can't be delivered.
    early = await client.post(f"/v1/riders/me/deliveries/{delivery_id}/delivered", headers=me)
    assert early.status_code == 409
    resp = await client.post(f"/v1/riders/me/deliveries/{delivery_id}/picked-up", headers=me)
    assert resp.status_code == 202
    assert env.signals == [("o-1", OrderSagaSignal.PICKED_UP, ())]


# ---------- Delivery lookup ----------


async def test_delivery_by_order_visibility(env: Env, client: AsyncClient) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    await add_rider(env, "r2", at=km_north(3))
    delivery_id, _ = await assigned(env)
    path = "/v1/deliveries/by-order/o-1"

    resp = await client.get(path, headers=_as(CUSTOMER, Role.CUSTOMER))
    assert (resp.status_code, resp.json()["id"], resp.json()["status"]) == (
        200,
        delivery_id,
        "assigned",
    )
    assert (await client.get(path, headers=_as("user-r1"))).status_code == 200
    assert (await client.get(path, headers=_as("u-admin", Role.SYSTEM_ADMIN))).status_code == 200
    assert (await client.get(path, headers=_as("someone", Role.CUSTOMER))).status_code == 403
    assert (await client.get(path, headers=_as("user-r2"))).status_code == 403
    missing = await client.get("/v1/deliveries/by-order/nope", headers=_as(CUSTOMER, Role.CUSTOMER))
    assert missing.status_code == 404
