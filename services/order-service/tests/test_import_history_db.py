"""The demo history import (app/import_history.py) and the summary that
restaurant-service checks reviews against."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app import import_history as import_module
from app import repository
from app.import_history import ImportedItem, ImportedOrder, import_history
from app.main import app
from app.models import Order, OrderStatus, OutboxEvent
from app.repository import apply_transition
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

PLACED = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)


def _order(order_id: str) -> ImportedOrder:
    return ImportedOrder(
        id=order_id,
        customer_id="u-customer",
        restaurant_id="r-1",
        restaurant_admin_id="u-owner",
        restaurant_lat=24.86,
        restaurant_lng=67.01,
        delivery_address_text="45 Clifton Road",
        delivery_lat=24.8138,
        delivery_lng=67.03,
        items=[
            ImportedItem(menu_item_id="m-1", name="Biryani", quantity=2, unit_price=Decimal("650")),
            ImportedItem(menu_item_id="m-2", name="Raita", quantity=1, unit_price=Decimal("120")),
            ImportedItem(menu_item_id="m-1", name="Biryani", quantity=1, unit_price=Decimal("650")),
        ],
        placed_at=PLACED,
    )


async def test_import_is_rerunnable_and_priced(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(import_module, "SessionFactory", session_factory)
    assert await import_history([_order("h-1"), _order("h-2")]) == 2
    assert await import_history([_order("h-1")]) == 0

    async with session_factory() as session:
        order = await session.get(Order, "h-1")
        assert order is not None
        assert order.status == "delivered"
        assert order.subtotal == Decimal("2070.00")
        assert order.tax_amount == Decimal("103.50")
        # 100 base + 30/km for 5.5 km straight line.
        assert order.delivery_fee == Decimal("265.00")
        assert order.total_amount == Decimal("2438.50")
        assert order.created_at == PLACED
        assert order.delivered_at == PLACED + timedelta(minutes=45)
        trail = [(h.from_status, h.to_status) for h in order.status_history]
        assert trail[0] == (None, "created")
        assert trail[-1] == ("picked_up", "delivered")


async def test_summary_for_review_checks(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(import_module, "SessionFactory", session_factory)
    await import_history([_order("h-1")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/internal/orders/h-1/summary")
        missing = await c.get("/internal/orders/nope/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_id"] == "u-customer"
    assert body["restaurant_id"] == "r-1"
    assert body["status"] == "delivered"
    assert body["menu_item_ids"] == ["m-1", "m-2"]  # each item once, in order
    assert missing.status_code == 404


async def test_delivered_export_and_event_carry_the_items(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(import_module, "SessionFactory", session_factory)
    await import_history([_order("h-1")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        page = (await c.get("/internal/orders/delivered", params={"limit": 10})).json()
    assert page["next_cursor"] is None
    [exported] = page["items"]
    assert exported["customer_id"] == "u-customer"
    assert [(i["name"], i["quantity"]) for i in exported["items"]] == [
        ("Biryani", 2),
        ("Raita", 1),
        ("Biryani", 1),
    ]

    # The live path: OrderDelivered written by a transition carries the same lines.
    async with session_factory() as session:
        order = await repository.get_order(session, "h-1")
        assert order is not None
        order.status = OrderStatus.PICKED_UP
        apply_transition(
            session, order, OrderStatus.DELIVERED, actor_id=None, actor_role=None, reason=None
        )
        await session.commit()
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "OrderDelivered")
            )
        ).scalar_one()
    assert [line["name"] for line in row.payload["items"]] == ["Biryani", "Raita", "Biryani"]
    assert row.payload["items"][0]["unit_price"] == "650.00"
