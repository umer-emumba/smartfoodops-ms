"""Reviews against a real Postgres (week4-plan step 1): who may review what,
one review per order, and the rating aggregate under concurrent reviews.
order-service is replaced by a dict of order summaries."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from app import import_reviews as import_module
from app import order_client
from app.config import settings
from app.db import get_session
from app.import_reviews import ImportedReview, import_reviews
from app.main import app
from app.models import Restaurant
from app.order_client import OrderSummary
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]
RID = "r-1"
OTHER_RID = "r-2"


def _as(user_id: str, role: Role) -> dict[str, str]:
    token = make_test_token(
        user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url, ttl_minutes=5
    )
    return {"Authorization": f"Bearer {token}"}


CUSTOMER = _as("u-customer", Role.CUSTOMER)
OTHER_CUSTOMER = _as("u-other", Role.CUSTOMER)
OWNER = _as("u-owner", Role.RESTAURANT_ADMIN)


def _order(order_id: str, *, status: str = "delivered", restaurant_id: str = RID) -> OrderSummary:
    return OrderSummary(
        id=order_id,
        customer_id="u-customer",
        restaurant_id=restaurant_id,
        status=status,
        menu_item_ids=["m-1", "m-2"],
    )


@pytest_asyncio.fixture
async def client(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    async with session_factory() as session:
        for rid in (RID, OTHER_RID):
            session.add(
                Restaurant(
                    id=rid,
                    admin_user_id="u-owner",
                    name=f"Kitchen {rid}",
                    address="1 Test St",
                    city="Karachi",
                    lat=24.86,
                    lng=67.01,
                    contact_phone="+923000000000",
                    status="active",
                )
            )
        await session.commit()

    orders = {
        o.id: o
        for o in [
            _order("o-1"),
            _order("o-2"),
            _order("o-pending", status="picked_up"),
            _order("o-elsewhere", restaurant_id=OTHER_RID),
            *(_order(f"o-c{i}") for i in range(10)),
        ]
    }

    async def get_order(order_id: str) -> OrderSummary | None:
        return orders.get(order_id)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(order_client, "get_order", get_order)
    monkeypatch.setattr(import_module, "SessionFactory", session_factory)
    app.dependency_overrides[get_session] = _get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


async def _review(client: AsyncClient, order_id: str, *, rating: int = 5, headers=CUSTOMER):
    return await client.post(
        f"/v1/restaurants/{RID}/reviews",
        json={"order_id": order_id, "rating": rating, "text": "Zabardast biryani"},
        headers=headers,
    )


async def _restaurant(client: AsyncClient) -> dict:
    return (await client.get(f"/v1/restaurants/{RID}")).json()


async def test_review_a_delivered_order(client: AsyncClient) -> None:
    resp = await _review(client, "o-1", rating=4)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["item_ids"] == ["m-1", "m-2"]  # from the order, not the client
    assert "customer_id" not in body

    await _review(client, "o-2", rating=5)
    restaurant = await _restaurant(client)
    assert restaurant["review_count"] == 2
    assert restaurant["avg_rating"] == 4.5

    page = (await client.get(f"/v1/restaurants/{RID}/reviews", params={"limit": 1})).json()
    assert [r["order_id"] for r in page["items"]] == ["o-2"]
    nxt = await client.get(
        f"/v1/restaurants/{RID}/reviews", params={"limit": 1, "cursor": page["next_cursor"]}
    )
    assert [r["order_id"] for r in nxt.json()["items"]] == ["o-1"]


async def test_one_review_per_order(client: AsyncClient) -> None:
    assert (await _review(client, "o-1")).status_code == 201
    assert (await _review(client, "o-1", rating=1)).status_code == 409
    assert (await _restaurant(client))["review_count"] == 1


async def test_who_may_review_what(client: AsyncClient) -> None:
    # Unknown and someone else's order look the same.
    assert (await _review(client, "o-missing")).status_code == 404
    assert (await _review(client, "o-1", headers=OTHER_CUSTOMER)).status_code == 404
    assert (await _review(client, "o-pending")).status_code == 409
    assert (await _review(client, "o-elsewhere")).status_code == 422
    assert (await _review(client, "o-1", headers=OWNER)).status_code == 403
    assert (await _restaurant(client))["review_count"] == 0


async def test_concurrent_reviews_all_counted(client: AsyncClient) -> None:
    results = await asyncio.gather(
        *(_review(client, f"o-c{i}", rating=1 + i % 5) for i in range(10))
    )
    assert [r.status_code for r in results] == [201] * 10
    restaurant = await _restaurant(client)
    assert restaurant["review_count"] == 10
    assert restaurant["avg_rating"] == 3.0


async def test_import_is_rerunnable_and_rebuilds_the_aggregate(client: AsyncClient) -> None:
    await _review(client, "o-1", rating=5)
    imported = [
        ImportedReview(
            order_id=f"h-{i}",
            customer_id="u-past",
            restaurant_id=RID,
            rating=2,
            text="Cold on arrival",
            item_ids=["m-1"],
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        for i in range(3)
    ]
    assert await import_reviews(imported) == 3
    assert await import_reviews(imported) == 0
    restaurant = await _restaurant(client)
    assert restaurant["review_count"] == 4
    assert restaurant["avg_rating"] == 2.75
