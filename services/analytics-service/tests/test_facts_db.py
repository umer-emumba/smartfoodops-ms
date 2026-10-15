"""Events → read model (app/facts.py) against a real Mongo, including that a
replay changes neither the documents nor the Prometheus counters."""

from decimal import Decimal
from typing import Any

import pytest
from app import facts
from app.mongo import FAILED_EVENTS, ORDER_FACTS, RIDER_SHIFT_FACTS
from prometheus_client import REGISTRY
from pymongo.asynchronous.database import AsyncDatabase
from sfo_events import DomainEvent, EventType

from tests.events import at, delivery, event, order, shift

pytestmark = pytest.mark.db

E = EventType


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def _apply(db: AsyncDatabase, events: list[DomainEvent]) -> None:
    for ev in events:
        await facts.apply_event(db, ev)


async def _fact(db: AsyncDatabase, order_id: str = "o-1") -> dict[str, Any]:
    doc = await db[ORDER_FACTS].find_one({"_id": order_id})
    assert doc is not None
    return doc


def _delivered_order() -> list[DomainEvent]:
    return [
        order(E.ORDER_PLACED, 0),
        order(E.ORDER_AWAITING_RESTAURANT, 1),
        order(E.ORDER_CONFIRMED, 2, from_status="awaiting_restaurant_acceptance"),
        delivery("offered", 3),
        delivery("assigned", 4),
        order(E.RIDER_ASSIGNED, 4, from_status="confirmed"),
        order(E.ORDER_PICKED_UP, 20, from_status="assigned"),
        delivery("delivered", 30),
        order(E.ORDER_DELIVERED, 30, from_status="picked_up"),
    ]


async def test_an_order_lifecycle_becomes_one_fact(db: AsyncDatabase) -> None:
    await _apply(db, _delivered_order())
    fact = await _fact(db)
    assert (fact["restaurant_id"], fact["restaurant_admin_id"]) == ("r-1", "u-owner")
    assert (fact["placed_at"], fact["confirmed_at"], fact["delivered_at"]) == (at(0), at(2), at(30))
    assert (fact["delivery_rider_id"], fact["delivery_assigned_at"]) == ("rider-1", at(4))
    assert fact["delivery_ended_at"] == at(30)
    assert fact["total_amount"].to_decimal() == Decimal("1250.00")


async def test_replay_changes_neither_facts_nor_counters(db: AsyncDatabase) -> None:
    events = _delivered_order()
    placed = _sample("orders_placed_total", restaurant_id="r-1")
    accepted = _sample("restaurant_responses_total", restaurant_id="r-1", response="accepted")
    delivered = _sample("order_outcomes_total", outcome="delivered", reason="")
    durations = _sample("delivery_duration_seconds_count")

    await _apply(db, events)
    first = await _fact(db)
    await _apply(db, events)  # offsets reset to earliest

    assert await _fact(db) == first
    assert _sample("orders_placed_total", restaurant_id="r-1") == placed + 1
    assert (
        _sample("restaurant_responses_total", restaurant_id="r-1", response="accepted")
        == accepted + 1
    )
    assert _sample("order_outcomes_total", outcome="delivered", reason="") == delivered + 1
    assert _sample("delivery_duration_seconds_count") == durations + 1


async def test_a_delivery_event_before_order_placed_still_merges(db: AsyncDatabase) -> None:
    # Different topics: no ordering guarantee between them.
    await _apply(db, [delivery("offered", 3), order(E.ORDER_PLACED, 0)])
    fact = await _fact(db)
    assert (fact["placed_at"], fact["delivery_offered_at"]) == (at(0), at(3))


async def test_reassignment_keeps_the_replacement_rider_and_counts_once(
    db: AsyncDatabase,
) -> None:
    dropped = delivery("pending_assignment", 10, rider_id=None, reason="rider_dropped")
    events = [
        delivery("assigned", 4, rider_id="rider-1"),
        dropped,
        delivery("assigned", 12, rider_id="rider-2"),
    ]
    await _apply(db, [*events, dropped])
    fact = await _fact(db)
    assert (fact["delivery_rider_id"], fact["delivery_assigned_at"]) == ("rider-2", at(12))
    assert fact["reassignments"] == [dropped.event_id]


async def test_rejection_reason_is_stored_as_its_code(db: AsyncDatabase) -> None:
    before = _sample("restaurant_responses_total", restaurant_id="r-9", response="rejected")
    await _apply(
        db,
        [
            order(E.ORDER_PLACED, 0, restaurant_id="r-9"),
            order(
                E.ORDER_REJECTED,
                3,
                restaurant_id="r-9",
                reason="restaurant_rejected: kitchen closed",
            ),
        ],
    )
    assert (await _fact(db))["reject_reason"] == "restaurant_rejected"
    assert (
        _sample("restaurant_responses_total", restaurant_id="r-9", response="rejected")
        == before + 1
    )


async def test_free_text_cancellation_reason_has_a_bounded_label(db: AsyncDatabase) -> None:
    before = _sample("order_outcomes_total", outcome="cancelled", reason="other")
    await facts.apply_event(db, order(E.ORDER_CANCELLED, 5, reason="changed my mind"))
    assert _sample("order_outcomes_total", outcome="cancelled", reason="other") == before + 1
    assert (await _fact(db))["cancel_reason"] == "changed my mind"


async def test_refunds_keep_the_highest_total(db: AsyncDatabase) -> None:
    def refund(total: str, minute: float) -> DomainEvent:
        return event(
            E.REFUND_PROCESSED,
            {
                "payment_id": "p-1",
                "order_id": "o-1",
                "customer_id": "u-customer",
                "refund_id": f"rf-{total}",
                "amount": total,
                "total_refunded": total,
                "reason": "cold",
            },
            minute,
        )

    await _apply(db, [refund("300.00", 40), refund("100.00", 41)])
    assert (await _fact(db))["total_refunded"].to_decimal() == Decimal("300.00")


async def test_shift_changes_are_recorded_once(db: AsyncDatabase) -> None:
    on = shift("rider-1", True, 0)
    before = _sample("rider_shift_changes_total", on_shift="true")
    await _apply(db, [on, on])
    assert await db[RIDER_SHIFT_FACTS].count_documents({}) == 1
    assert _sample("rider_shift_changes_total", on_shift="true") == before + 1


class _DeadLetter:
    def topic(self) -> str:
        return "order-events.dlq"

    def partition(self) -> int:
        return 1

    def offset(self) -> int:
        return 7

    def timestamp(self) -> tuple[int, int]:
        return (1, int(at(5).timestamp() * 1000))

    def headers(self) -> list[tuple[str, bytes]]:
        return [
            ("event_type", b"OrderConfirmed"),
            ("dlq.original.topic", b"order-events"),
            ("dlq.consumer.group", b"notification-service"),
            ("dlq.error.type", b"ValueError"),
            ("dlq.error.message", b"bad payload"),
        ]


async def test_dead_letters_are_counted_once(db: AsyncDatabase) -> None:
    before = _sample(
        "dead_lettered_events_total", topic="order-events", consumer_group="notification-service"
    )
    await facts.record_dead_letter(db, _DeadLetter())  # type: ignore[arg-type]
    await facts.record_dead_letter(db, _DeadLetter())  # type: ignore[arg-type]
    [doc] = await db[FAILED_EVENTS].find().to_list()
    assert doc["_id"] == "order-events.dlq:1:7"
    assert (doc["event_type"], doc["error_type"], doc["at"]) == (
        "OrderConfirmed",
        "ValueError",
        at(5),
    )
    assert (
        _sample(
            "dead_lettered_events_total",
            topic="order-events",
            consumer_group="notification-service",
        )
        == before + 1
    )
