"""Restaurant-admin views, restaurant response and cancel endpoints
(week2-plan step 1) against a real Postgres; signals to the saga are
captured (`signals` fixture)."""

import pytest
from app.config import settings
from app.main import app
from app.models import OrderStatus
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token
from sfo_contracts import OrderSagaSignal
from sfo_contracts.activities import Actor
from sfo_contracts.workflows import CancelRequest, RestaurantResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.db_helpers import CUSTOMER, OWNER, RESTAURANT, insert_order

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


def _as(user_id: str, role: Role) -> dict[str, str]:
    token = create_access_token(
        user_id=user_id,
        role=role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=5,
    )
    return {"Authorization": f"Bearer {token}"}


OWNER_H = _as(OWNER, Role.RESTAURANT_ADMIN)
OTHER_ADMIN_H = _as("u-other-owner", Role.RESTAURANT_ADMIN)
CUSTOMER_H = _as(CUSTOMER, Role.CUSTOMER)
OTHER_CUSTOMER_H = _as("u-someone-else", Role.CUSTOMER)
SYSTEM_ADMIN_H = _as("u-admin", Role.SYSTEM_ADMIN)


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------- Restaurant order queue ----------


async def test_owner_sees_their_queue_filtered_by_status(
    session_factory: Factory, client: AsyncClient
) -> None:
    waiting = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    await insert_order(session_factory, status=OrderStatus.DELIVERED)
    await insert_order(session_factory, restaurant_id="r-2", restaurant_admin_id="u-other-owner")

    resp = await client.get(
        f"/v1/orders/restaurant/{RESTAURANT}",
        params={"status": "awaiting_restaurant_acceptance"},
        headers=OWNER_H,
    )
    assert resp.status_code == 200
    assert [o["id"] for o in resp.json()["items"]] == [waiting.id]

    resp = await client.get(f"/v1/orders/restaurant/{RESTAURANT}", headers=OWNER_H)
    assert len(resp.json()["items"]) == 2


async def test_queue_is_owner_or_system_admin_only(
    session_factory: Factory, client: AsyncClient
) -> None:
    await insert_order(session_factory)
    path = f"/v1/orders/restaurant/{RESTAURANT}"
    assert (await client.get(path, headers=OTHER_ADMIN_H)).status_code == 403
    assert (await client.get(path, headers=CUSTOMER_H)).status_code == 403
    resp = await client.get(path, headers=SYSTEM_ADMIN_H)
    assert (resp.status_code, len(resp.json()["items"])) == (200, 1)


async def test_owner_can_read_order_detail_other_admin_cannot(
    session_factory: Factory, client: AsyncClient
) -> None:
    order = await insert_order(session_factory)
    assert (await client.get(f"/v1/orders/{order.id}", headers=OWNER_H)).status_code == 200
    assert (await client.get(f"/v1/orders/{order.id}", headers=OTHER_ADMIN_H)).status_code == 403


# ---------- Restaurant response ----------


async def test_owner_response_signals_the_saga(
    session_factory: Factory, client: AsyncClient, signals: list
) -> None:
    order = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    resp = await client.post(
        f"/v1/orders/{order.id}/restaurant-response",
        json={"decision": "reject", "reason": "Kitchen closed"},
        headers=OWNER_H,
    )
    assert resp.status_code == 202
    # Unchanged until the saga applies it.
    assert resp.json()["status"] == "awaiting_restaurant_acceptance"
    assert signals == [
        (
            order.id,
            OrderSagaSignal.RESTAURANT_RESPONSE,
            RestaurantResponse(decision="reject", reason="Kitchen closed"),
        )
    ]


async def test_response_from_another_restaurant_is_forbidden(
    session_factory: Factory, client: AsyncClient, signals: list
) -> None:
    order = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    resp = await client.post(
        f"/v1/orders/{order.id}/restaurant-response",
        json={"decision": "accept"},
        headers=OTHER_ADMIN_H,
    )
    assert resp.status_code == 403
    assert signals == []


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.CONFIRMED])
async def test_response_only_while_awaiting(
    session_factory: Factory, client: AsyncClient, signals: list, status: OrderStatus
) -> None:
    order = await insert_order(session_factory, status=status)
    resp = await client.post(
        f"/v1/orders/{order.id}/restaurant-response", json={"decision": "accept"}, headers=OWNER_H
    )
    assert resp.status_code == 409
    assert signals == []


async def test_response_validates_decision(session_factory: Factory, client: AsyncClient) -> None:
    order = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    resp = await client.post(
        f"/v1/orders/{order.id}/restaurant-response", json={"decision": "maybe"}, headers=OWNER_H
    )
    assert resp.status_code == 422


# ---------- Cancel ----------


async def test_customer_cancel_signals_the_saga(
    session_factory: Factory, client: AsyncClient, signals: list
) -> None:
    order = await insert_order(session_factory, status=OrderStatus.ASSIGNED)
    resp = await client.post(
        f"/v1/orders/{order.id}/cancel", json={"reason": "Changed my mind"}, headers=CUSTOMER_H
    )
    assert resp.status_code == 202
    assert signals == [
        (
            order.id,
            OrderSagaSignal.CANCEL,
            CancelRequest(actor=Actor(id=CUSTOMER, role="customer"), reason="Changed my mind"),
        )
    ]


async def test_cancel_by_another_customer_is_forbidden(
    session_factory: Factory, client: AsyncClient, signals: list
) -> None:
    order = await insert_order(session_factory)
    resp = await client.post(f"/v1/orders/{order.id}/cancel", json={}, headers=OTHER_CUSTOMER_H)
    assert resp.status_code == 403
    assert signals == []


async def test_system_admin_can_cancel(
    session_factory: Factory, client: AsyncClient, signals: list
) -> None:
    order = await insert_order(session_factory)
    resp = await client.post(f"/v1/orders/{order.id}/cancel", json={}, headers=SYSTEM_ADMIN_H)
    assert resp.status_code == 202
    assert signals[0][2].actor == Actor(id="u-admin", role="system_admin")


@pytest.mark.parametrize(
    "status", [OrderStatus.PICKED_UP, OrderStatus.DELIVERED, OrderStatus.REJECTED]
)
async def test_cancel_not_allowed_from_pickup_on(
    session_factory: Factory, client: AsyncClient, signals: list, status: OrderStatus
) -> None:
    order = await insert_order(session_factory, status=status)
    resp = await client.post(f"/v1/orders/{order.id}/cancel", json={}, headers=CUSTOMER_H)
    assert resp.status_code == 409
    assert signals == []


async def test_unknown_order_is_404(session_factory: Factory, client: AsyncClient) -> None:
    resp = await client.post("/v1/orders/nope/cancel", json={}, headers=CUSTOMER_H)
    assert resp.status_code == 404
