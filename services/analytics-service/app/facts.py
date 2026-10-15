"""Applying events to the read model (app/mongo.py, docs/week3-plan.md
W3-D9).

Every write is idempotent: milestone timestamps use `$min` (the first time
wins, a replay changes nothing), the latest delivery assignment uses `$set`
(events of one delivery arrive in order), refunds `$max`, reassignments
`$addToSet` of the event ID. Each write returns the document as it was
*before*, which is how the Prometheus counters (app/business_metrics.py)
tell a first application from a replay.

Order events, delivery events and refunds arrive on different topics, so
their order across topics isn't guaranteed — each kind only touches its own
fields, and an order's document is created by whichever arrives first."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from bson import Decimal128
from confluent_kafka import Message
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from sfo_events import DomainEvent, EventType
from sfo_events.kafka import (
    HEADER_DLQ_ERROR,
    HEADER_DLQ_ERROR_TYPE,
    HEADER_DLQ_GROUP,
    HEADER_DLQ_TOPIC,
    HEADER_EVENT_TYPE,
    header_value,
)
from sfo_events.payloads import (
    DeliveryStatusUpdatedPayload,
    OrderPlacedPayload,
    OrderTransitionPayload,
    RefundProcessedPayload,
    RiderShiftChangedPayload,
)

from app import business_metrics as m
from app.mongo import FAILED_EVENTS, ORDER_FACTS, RIDER_SHIFT_FACTS

# The milestone each order event records.
ORDER_MILESTONES: dict[EventType, str] = {
    EventType.ORDER_PLACED: "placed_at",
    EventType.ORDER_AWAITING_RESTAURANT: "sent_to_restaurant_at",
    EventType.ORDER_CONFIRMED: "confirmed_at",
    EventType.ORDER_REJECTED: "rejected_at",
    EventType.ORDER_CANCELLED: "cancelled_at",
    EventType.RIDER_ASSIGNED: "assigned_at",
    EventType.ORDER_PICKED_UP: "picked_up_at",
    EventType.ORDER_DELIVERED: "delivered_at",
}

# Rejections that were the restaurant's decision — the acceptance-rate
# denominator (stock and payment failures never reach the restaurant).
RESTAURANT_REJECTIONS = ("restaurant_rejected", "restaurant_timeout")


def _decimal(value: Decimal) -> Decimal128:
    return Decimal128(str(value))


async def _upsert(
    db: AsyncDatabase, collection: str, key: str, update: dict[str, Any]
) -> dict[str, Any] | None:
    """The document before the update; None if this created it."""
    before: dict[str, Any] | None = await db[collection].find_one_and_update(
        {"_id": key}, update, upsert=True, return_document=ReturnDocument.BEFORE
    )
    return before


def _is_new(before: dict[str, Any] | None, field: str) -> bool:
    return before is None or before.get(field) is None


async def _order_event(
    db: AsyncDatabase, event: DomainEvent, p: OrderPlacedPayload | OrderTransitionPayload
) -> None:
    milestone = ORDER_MILESTONES[event.event_type]
    at = event.occurred_at
    fields: dict[str, Any] = {
        "order_number": p.order_number,
        "customer_id": p.customer_id,
        "restaurant_id": p.restaurant_id,
        "restaurant_admin_id": p.restaurant_admin_id,
    }
    if isinstance(p, OrderTransitionPayload):
        # OrderPlaced's total is the pre-pricing subtotal; later events
        # carry the final one.
        fields["total_amount"] = _decimal(p.total_amount)
        if event.event_type == EventType.ORDER_REJECTED:
            fields["reject_reason"] = m.reason_code(p.reason)
        elif event.event_type == EventType.ORDER_CANCELLED:
            fields["cancel_reason"] = m.reason_code(p.reason)
            fields["cancelled_from"] = p.from_status
    before = await _upsert(db, ORDER_FACTS, p.order_id, {"$set": fields, "$min": {milestone: at}})
    if not _is_new(before, milestone):
        return

    restaurant = p.restaurant_id
    reason = getattr(p, "reason", None)
    if event.event_type == EventType.ORDER_PLACED:
        m.ORDERS_PLACED.labels(restaurant).inc()
    elif event.event_type == EventType.ORDER_CONFIRMED:
        m.RESTAURANT_RESPONSES.labels(restaurant, "accepted").inc()
    elif event.event_type == EventType.ORDER_REJECTED:
        m.ORDER_OUTCOMES.labels("rejected", m.reason_label(reason)).inc()
        code = m.reason_code(reason)
        if code in RESTAURANT_REJECTIONS:
            response = "timeout" if code == "restaurant_timeout" else "rejected"
            m.RESTAURANT_RESPONSES.labels(restaurant, response).inc()
    elif event.event_type == EventType.ORDER_CANCELLED:
        m.ORDER_OUTCOMES.labels("cancelled", m.reason_label(reason)).inc()
    elif event.event_type == EventType.ORDER_DELIVERED:
        m.ORDER_OUTCOMES.labels("delivered", "").inc()
        placed_at = before.get("placed_at") if before else None
        if placed_at is not None:
            m.DELIVERY_DURATION.observe((at - placed_at).total_seconds())


async def _delivery_event(
    db: AsyncDatabase, event: DomainEvent, p: DeliveryStatusUpdatedPayload
) -> None:
    at = event.occurred_at
    update: dict[str, dict[str, Any]] = {"$set": {"delivery_id": p.delivery_id}}
    if p.to_status == "offered":
        update["$min"] = {"delivery_offered_at": at}
    elif p.to_status == "assigned":
        # The latest assignment: after a drop-out, the replacement rider.
        update["$set"] |= {"delivery_rider_id": p.rider_id, "delivery_assigned_at": at}
    elif p.to_status in ("delivered", "cancelled"):
        update["$min"] = {"delivery_ended_at": at}
    elif p.to_status == "pending_assignment" and (p.reason or "").startswith("rider_"):
        update["$addToSet"] = {"reassignments": event.event_id}
    await _upsert(db, ORDER_FACTS, p.order_id, update)


async def _refund(db: AsyncDatabase, _: DomainEvent, p: RefundProcessedPayload) -> None:
    await _upsert(
        db, ORDER_FACTS, p.order_id, {"$max": {"total_refunded": _decimal(p.total_refunded)}}
    )


async def _shift(db: AsyncDatabase, event: DomainEvent, p: RiderShiftChangedPayload) -> None:
    before = await _upsert(
        db,
        RIDER_SHIFT_FACTS,
        event.event_id,
        {
            "$setOnInsert": {
                "rider_id": p.rider_id,
                "rider_user_id": p.rider_user_id,
                "on_shift": p.on_shift,
                "reason": p.reason,
                "at": event.occurred_at,
            }
        },
    )
    if before is None:
        m.RIDER_SHIFT_CHANGES.labels(str(p.on_shift).lower()).inc()


_HANDLERS: dict[EventType, Callable[[AsyncDatabase, DomainEvent, Any], Awaitable[None]]] = {
    **dict.fromkeys(ORDER_MILESTONES, _order_event),
    EventType.DELIVERY_STATUS_UPDATED: _delivery_event,
    EventType.REFUND_PROCESSED: _refund,
    EventType.RIDER_SHIFT_CHANGED: _shift,
    # PaymentSuccessful: no metric needs it.
}


async def apply_event(db: AsyncDatabase, event: DomainEvent) -> None:
    handler = _HANDLERS.get(event.event_type)
    if handler is not None:
        await handler(db, event, event.typed_payload())


async def record_dead_letter(db: AsyncDatabase, msg: Message) -> None:
    """One document per DLQ message, keyed by its position in the DLQ."""
    headers = msg.headers()
    _, timestamp_ms = msg.timestamp()
    original_topic = header_value(headers, HEADER_DLQ_TOPIC) or ""
    group = header_value(headers, HEADER_DLQ_GROUP) or ""
    before = await _upsert(
        db,
        FAILED_EVENTS,
        f"{msg.topic()}:{msg.partition()}:{msg.offset()}",
        {
            "$setOnInsert": {
                "original_topic": original_topic,
                "consumer_group": group,
                "event_type": header_value(headers, HEADER_EVENT_TYPE),
                "error_type": header_value(headers, HEADER_DLQ_ERROR_TYPE),
                "error": header_value(headers, HEADER_DLQ_ERROR),
                "at": datetime.fromtimestamp(timestamp_ms / 1000, UTC),
            }
        },
    )
    if before is None:
        m.DEAD_LETTERED.labels(original_topic, group).inc()
