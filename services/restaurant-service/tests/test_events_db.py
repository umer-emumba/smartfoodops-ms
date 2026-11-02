"""restaurant-events in the outbox (week4-plan step 2): which writes produce
which events, the snapshots they carry, and source_version rising with each
transaction. Against a real Postgres; nothing is published here (that's the
relay's job, tested in libs/events)."""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app import activities, order_client
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import OutboxEvent, Restaurant
from app.order_client import OrderSummary
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sfo_contracts.activities import OrderRef, ReservedItem, ReserveOrderInput
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]
RID = "r-1"


def _as(user_id: str, role: Role) -> dict[str, str]:
    token = make_test_token(
        user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url, ttl_minutes=5
    )
    return {"Authorization": f"Bearer {token}"}


OWNER = _as("u-owner", Role.RESTAURANT_ADMIN)
ADMIN = _as("u-admin", Role.SYSTEM_ADMIN)
CUSTOMER = _as("u-customer", Role.CUSTOMER)


@pytest_asyncio.fixture
async def client(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    async with session_factory() as session:
        session.add(
            Restaurant(
                id=RID,
                admin_user_id="u-owner",
                name="Desi Dastarkhwan",
                address="Jinnah Super, F-7",
                city="Islamabad",
                lat=33.72,
                lng=73.05,
                contact_phone="+923000000000",
                cuisine_type="Pakistani",
                status="active",
            )
        )
        await session.commit()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def get_order(order_id: str) -> OrderSummary:
        return OrderSummary(order_id, "u-customer", RID, "delivered", ["m-1"])

    monkeypatch.setattr(order_client, "get_order", get_order)
    app.dependency_overrides[get_session] = _get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


async def _events(factory: Factory) -> list[tuple[str, dict]]:
    async with factory() as session:
        rows = await session.execute(
            select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
        )
        events = [(row.event_type, row.payload) for row in rows.scalars()]
    assert all(p["restaurant_id"] == RID for _, p in events)
    return events


async def _menu(client: AsyncClient, **item) -> tuple[str, str]:
    base = f"/v1/restaurants/{RID}/menu"
    category = await client.post(f"{base}/categories", json={"name": "Rice"}, headers=OWNER)
    resp = await client.post(
        f"{base}/items",
        json={
            "category_id": category.json()["id"],
            "name": "Chicken Biryani",
            "price": 690,
            **item,
        },
        headers=OWNER,
    )
    assert resp.status_code == 201, resp.text
    return category.json()["id"], resp.json()["id"]


async def test_item_events_carry_a_full_snapshot(client: AsyncClient, session_factory) -> None:
    category_id, item_id = await _menu(
        client, dietary_tags=["halal"], spice_level=2, calories=820, stock_count=5
    )
    await client.patch(
        f"/v1/restaurants/{RID}/menu/items/{item_id}", json={"price": 720}, headers=OWNER
    )
    await client.delete(f"/v1/restaurants/{RID}/menu/items/{item_id}", headers=OWNER)

    events = await _events(session_factory)
    assert [t for t, _ in events] == ["MenuItemUpserted", "MenuItemUpserted", "MenuItemDeleted"]
    created = events[0][1]
    assert created["restaurant_name"] == "Desi Dastarkhwan"
    assert created["restaurant_status"] == "active"
    assert created["category_name"] == "Rice"
    assert created["city"] == "Islamabad"
    assert created["dietary_tags"] == ["halal"]
    assert created["in_stock"] is True
    assert events[1][1]["price"] == "720.00"
    versions = [p["source_version"] for _, p in events]
    assert versions == sorted(set(versions))  # strictly rising


async def test_restaurant_rename_resends_its_items(client: AsyncClient, session_factory) -> None:
    await _menu(client)
    await client.patch(f"/v1/restaurants/{RID}", json={"description": "Dum biryani"}, headers=OWNER)
    await client.patch(f"/v1/restaurants/{RID}", json={"name": "Dastarkhwan"}, headers=OWNER)
    # Unchanged values aren't a change.
    await client.patch(f"/v1/restaurants/{RID}", json={"name": "Dastarkhwan"}, headers=OWNER)

    types = [t for t, _ in await _events(session_factory)]
    assert types == [
        "MenuItemUpserted",
        "RestaurantUpserted",  # description: only the restaurant
        "RestaurantUpserted",  # name: the restaurant and every item
        "MenuItemUpserted",
        "RestaurantUpserted",
    ]
    events = await _events(session_factory)
    assert events[3][1]["restaurant_name"] == "Dastarkhwan"
    assert events[2][1]["source_version"] == events[3][1]["source_version"]


async def test_category_rename_resends_its_items(client: AsyncClient, session_factory) -> None:
    category_id, _ = await _menu(client)
    await client.patch(
        f"/v1/restaurants/{RID}/menu/categories/{category_id}",
        json={"name": "Biryani & Pulao"},
        headers=OWNER,
    )
    events = await _events(session_factory)
    assert [t for t, _ in events] == ["MenuItemUpserted", "MenuItemUpserted"]
    assert events[1][1]["category_name"] == "Biryani & Pulao"


async def test_status_change_and_review(client: AsyncClient, session_factory) -> None:
    await client.post(f"/v1/restaurants/{RID}/suspend", json={"reason": "audit"}, headers=ADMIN)
    await client.post(f"/v1/restaurants/{RID}/suspend", headers=ADMIN)  # no-op
    await client.post(f"/v1/restaurants/{RID}/approve", headers=ADMIN)
    resp = await client.post(
        f"/v1/restaurants/{RID}/reviews",
        json={"order_id": "o-1", "rating": 4, "text": "Proper dum"},
        headers=CUSTOMER,
    )
    assert resp.status_code == 201

    events = await _events(session_factory)
    assert [(t, p.get("to_status")) for t, p in events] == [
        ("RestaurantStatusChanged", "suspended"),
        ("RestaurantStatusChanged", "active"),
        ("ReviewSubmitted", None),
    ]
    assert events[0][1]["reason"] == "audit"
    review = events[2][1]
    assert (review["avg_rating"], review["review_count"]) == (4.0, 1)
    assert "customer_id" not in review


async def test_stock_events_only_when_in_stock_flips(client, session_factory) -> None:
    _, item_id = await _menu(client, stock_count=2)

    def reserve(order_id: str) -> ReserveOrderInput:
        return ReserveOrderInput(
            order_id=order_id,
            restaurant_id=RID,
            items=[ReservedItem(menu_item_id=item_id, quantity=1)],
        )

    monkey = pytest.MonkeyPatch()
    monkey.setattr(activities, "SessionFactory", session_factory)
    try:
        await activities.reserve_order(reserve("o-1"))  # 2 -> 1: nothing to tell
        await activities.reserve_order(reserve("o-2"))  # 1 -> 0: sold out
        await activities.release_reservation(OrderRef("o-2"))  # 0 -> 1: back
        await activities.release_reservation(OrderRef("o-1"))  # 1 -> 2: nothing
    finally:
        monkey.undo()

    events = await _events(session_factory)
    assert [(t, p["in_stock"]) for t, p in events] == [
        ("MenuItemUpserted", True),  # created
        ("MenuItemUpserted", False),
        ("MenuItemUpserted", True),
    ]


async def test_versions_rise_under_concurrent_writes(client, session_factory) -> None:
    _, item_id = await _menu(client)
    await asyncio.gather(
        *(
            client.patch(
                f"/v1/restaurants/{RID}/menu/items/{item_id}",
                json={"description": f"edit {i}"},
                headers=OWNER,
            )
            for i in range(8)
        )
    )
    versions = [p["source_version"] for _, p in await _events(session_factory)]
    assert len(versions) == len(set(versions))
