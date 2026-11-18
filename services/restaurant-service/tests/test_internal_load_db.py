"""POST /internal/restaurants/load (docs/week5-plan.md step 1): each
kitchen's active orders against its cap, for ai-service's suggestions."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.db import get_session
from app.main import app
from app.models import Restaurant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async with session_factory() as session:
        for rid, active, cap, accepting in [("r-1", 3, 10, True), ("r-2", 0, 20, False)]:
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
                    active_order_count=active,
                    max_concurrent_orders=cap,
                    is_accepting_orders=accepting,
                )
            )
        await session.commit()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_loads_for_known_restaurants_only(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/restaurants/load", json={"restaurant_ids": ["r-1", "r-2", "r-missing"]}
    )
    assert resp.status_code == 200
    loads = {row["restaurant_id"]: row for row in resp.json()["loads"]}
    assert loads == {
        "r-1": {
            "restaurant_id": "r-1",
            "active_orders": 3,
            "max_orders": 10,
            "is_accepting_orders": True,
        },
        "r-2": {
            "restaurant_id": "r-2",
            "active_orders": 0,
            "max_orders": 20,
            "is_accepting_orders": False,
        },
    }


async def test_ids_are_required(client: AsyncClient) -> None:
    resp = await client.post("/internal/restaurants/load", json={"restaurant_ids": []})
    assert resp.status_code == 422
