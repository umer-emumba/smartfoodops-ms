"""The metric definitions (app/queries.py) over a hand-built set of orders,
against a real Mongo."""

import pytest
from app import facts, queries
from app.queries import Window
from pymongo.asynchronous.database import AsyncDatabase
from sfo_events import EventType

from tests.events import at, delivery, order, shift

pytestmark = pytest.mark.db

E = EventType
WINDOW = Window(at(0), at(120))
NOW = at(120)


@pytest.fixture
async def orders(db: AsyncDatabase) -> AsyncDatabase:
    """o-1 delivered in 35 min; o-2 rejected by the restaurant; o-3 payment
    declined; o-4 cancelled after a rider accepted; o-5 a saga system error;
    o-6 placed outside the window."""
    events = [
        order(E.ORDER_PLACED, 0, order_id="o-1"),
        order(E.ORDER_CONFIRMED, 2, order_id="o-1"),
        delivery("assigned", 5, order_id="o-1", rider_id="rider-1"),
        delivery("delivered", 35, order_id="o-1", rider_id="rider-1"),
        order(E.ORDER_DELIVERED, 35, order_id="o-1"),
        order(E.ORDER_PLACED, 10, order_id="o-2"),
        order(E.ORDER_REJECTED, 12, order_id="o-2", reason="restaurant_rejected: closed"),
        order(E.ORDER_PLACED, 20, order_id="o-3", restaurant_id="r-2"),
        order(E.ORDER_REJECTED, 21, order_id="o-3", restaurant_id="r-2", reason="payment_declined"),
        order(E.ORDER_PLACED, 70, order_id="o-4", restaurant_id="r-2"),
        order(E.ORDER_CONFIRMED, 72, order_id="o-4", restaurant_id="r-2"),
        delivery("assigned", 75, order_id="o-4", rider_id="rider-1"),
        delivery("cancelled", 80, order_id="o-4", rider_id="rider-1"),
        order(E.ORDER_CANCELLED, 80, order_id="o-4", restaurant_id="r-2", from_status="confirmed"),
        order(E.ORDER_PLACED, 25, order_id="o-5"),
        order(E.ORDER_REJECTED, 26, order_id="o-5", reason="system_error"),
        order(E.ORDER_PLACED, 200, order_id="o-6"),
        # rider-1 on shift from before the window until 100; rider-2's "on"
        # predates our records (only the "off" is known); rider-3 still on.
        shift("rider-1", True, -30),
        shift("rider-1", False, 100),
        shift("rider-2", False, 50),
        shift("rider-3", True, 90),
    ]
    for ev in events:
        await facts.apply_event(db, ev)
    return db


async def test_order_metrics(orders: AsyncDatabase) -> None:
    stats = await queries.order_stats(orders, WINDOW)
    assert stats == {
        "total_orders": 5,
        "confirmed": 2,
        "rejected": 3,
        "cancelled": 1,
        "delivered": 1,
        "cancellation_rate": 0.2,
        # 2 confirmed ÷ (2 + 1 restaurant rejection); payment and system
        # failures never reached the restaurant.
        "restaurant_acceptance_rate": 0.6667,
        # 1 delivered of 2 a rider accepted.
        "delivery_success_rate": 0.5,
        "avg_delivery_minutes": 35.0,
        "saga_system_errors": 1,
    }


async def test_metrics_for_one_restaurant(orders: AsyncDatabase) -> None:
    stats = await queries.order_stats(orders, WINDOW, "r-2")
    assert (stats["total_orders"], stats["cancelled"], stats["restaurant_acceptance_rate"]) == (
        2,
        1,
        1.0,
    )


async def test_empty_window_has_no_rates(db: AsyncDatabase) -> None:
    stats = await queries.order_stats(db, WINDOW)
    assert stats["total_orders"] == 0
    assert stats["cancellation_rate"] is None and stats["avg_delivery_minutes"] is None


async def test_orders_per_restaurant(orders: AsyncDatabase) -> None:
    assert await queries.orders_per_restaurant(orders, WINDOW) == [
        {"restaurant_id": "r-1", "orders": 3},
        {"restaurant_id": "r-2", "orders": 2},
    ]


async def test_peak_hours_in_local_time(orders: AsyncDatabase) -> None:
    hours = await queries.peak_hours(orders, WINDOW, "Asia/Karachi")
    assert len(hours) == 24
    # T0 is 07:00 UTC = 12:00 in Karachi; o-4 was placed at 13:10.
    assert {h["hour"]: h["orders"] for h in hours if h["orders"]} == {12: 4, 13: 1}


async def test_rider_utilization(orders: AsyncDatabase) -> None:
    result = await queries.rider_utilization(orders, WINDOW, now=NOW)
    riders = {r["rider_id"]: r for r in result["riders"]}
    # rider-1: on shift 0–100 in the window (100 min), on deliveries
    # 5–35 and 75–80 (35 min).
    assert (riders["rider-1"]["on_shift_seconds"], riders["rider-1"]["on_delivery_seconds"]) == (
        6000.0,
        2100.0,
    )
    assert riders["rider-1"]["utilization"] == 0.35
    assert riders["rider-2"]["on_shift_seconds"] == 3000.0  # window start → off at 50
    assert riders["rider-3"]["on_shift_seconds"] == 1800.0  # 90 → now
    assert result["overall"] == round(2100 / (6000 + 3000 + 1800), 4)


async def test_failed_events_in_window(db: AsyncDatabase) -> None:
    await db["failed_events"].insert_many([{"_id": "a", "at": at(10)}, {"_id": "b", "at": at(500)}])
    assert await queries.failed_events(db, WINDOW) == 1
