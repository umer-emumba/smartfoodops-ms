"""Starting the saga (week2-plan step 4, D3/D4): POST /v1/orders starts it
after commit, and the reconciler starts it for orders that were committed
without one."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app import reconciler, restaurant_client, temporal_client
from app.config import settings
from app.main import app
from app.models import OrderStatus
from app.restaurant_client import MenuItemSnapshot, RestaurantSnapshot
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.db_helpers import insert_order

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Saga inputs passed to start_order_saga, instead of starting workflows."""
    inputs: list[Any] = []

    async def _start(_client: object, order: Any) -> bool:
        inputs.append(temporal_client.saga_input(order))
        return True

    async def _get_client() -> object:
        return object()

    monkeypatch.setattr(temporal_client, "start_order_saga", _start)
    monkeypatch.setattr(temporal_client, "get_client", _get_client)
    return inputs


# ---------- POST /v1/orders ----------


@pytest.fixture
def restaurant(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_quote(restaurant_id: str, item_ids: list[str]):  # type: ignore[no-untyped-def]
        return (
            RestaurantSnapshot(restaurant_id, "active", True, "u-owner", 33.6844, 73.0479),
            {"m-1": MenuItemSnapshot("m-1", "Biryani", Decimal("500"), True, 10)},
        )

    monkeypatch.setattr(restaurant_client, "get_quote", get_quote)


async def _place_order(key: str | None = None) -> Any:
    token = make_test_token(
        user_id="u-customer", role=Role.CUSTOMER, jwks_url=settings.jwt_jwks_url, ttl_minutes=5
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key or str(uuid.uuid4()),
            },
            json={
                "restaurant_id": "r-1",
                "delivery_address_text": "1 Test St",
                "delivery_lat": 33.7044,
                "delivery_lng": 73.0479,
                "promo_code": "WELCOME10",
                "payment_method": "card_simulated_decline",
                "items": [{"menu_item_id": "m-1", "quantity": 2}],
            },
        )


async def test_new_order_starts_its_saga(
    session_factory: Factory, restaurant: None, started: list
) -> None:
    resp = await _place_order()
    assert (resp.status_code, resp.json()["status"]) == (201, "created")
    [saga] = started
    assert saga.order_id == resp.json()["id"]
    assert (saga.subtotal, saga.promo_code, saga.payment_method) == (
        Decimal("1000.00"),
        "WELCOME10",
        "card_simulated_decline",
    )
    assert (saga.pickup.lat, saga.dropoff.lat) == (33.6844, 33.7044)
    assert [(i.menu_item_id, i.quantity) for i in saga.items] == [("m-1", 2)]
    assert saga.pricing.tax_rate == settings.tax_rate


async def test_order_is_accepted_even_if_temporal_is_down(
    session_factory: Factory, restaurant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down() -> object:
        raise RuntimeError("Temporal unreachable")

    monkeypatch.setattr(temporal_client, "get_client", _down)
    resp = await _place_order()
    # Committed; the reconciler starts the saga later.
    assert (resp.status_code, resp.json()["status"]) == (201, "created")


async def test_retried_request_starts_a_missing_saga(
    session_factory: Factory, restaurant: None, started: list
) -> None:
    key = str(uuid.uuid4())
    first = await _place_order(key)
    replay = await _place_order(key)
    assert (replay.status_code, replay.json()["id"]) == (200, first.json()["id"])
    assert len(started) == 2  # second start is a no-op in Temporal (same workflow ID)


# ---------- Reconciler ----------


async def test_reconciler_starts_sagas_for_stranded_orders_only(
    session_factory: Factory, started: list
) -> None:
    now = datetime.now(UTC)
    stranded = await insert_order(session_factory, created_at=now - timedelta(minutes=2))
    await insert_order(session_factory, created_at=now - timedelta(seconds=10))  # too new
    await insert_order(session_factory, created_at=now - timedelta(hours=2))  # abandoned
    await insert_order(
        session_factory,
        status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE,
        created_at=now - timedelta(minutes=2),
    )

    assert await reconciler.reconcile_once(object()) == 1  # type: ignore[arg-type]
    assert [s.order_id for s in started] == [stranded.id]
    assert await reconciler.count_abandoned() == 1
