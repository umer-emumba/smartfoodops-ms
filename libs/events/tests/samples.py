"""One valid payload per event type, shaped like the real writers' output."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sfo_events import DomainEvent, EventType

_ORDER = {
    "order_id": "ord-1",
    "order_number": 1001,
    "customer_id": "cust-1",
    "restaurant_id": "rest-1",
    "restaurant_admin_id": "radmin-1",
    "total_amount": Decimal("25.50"),
}


def _transition(from_status: str, to_status: str, reason: str | None = None) -> dict[str, Any]:
    return {**_ORDER, "from_status": from_status, "to_status": to_status, "reason": reason}


SAMPLE_PAYLOADS: dict[EventType, dict[str, Any]] = {
    EventType.ORDER_PLACED: {**_ORDER, "items": [{"menu_item_id": "item-1", "quantity": 2}]},
    EventType.ORDER_AWAITING_RESTAURANT: _transition("created", "awaiting_restaurant_acceptance"),
    EventType.ORDER_CONFIRMED: _transition("awaiting_restaurant_acceptance", "confirmed"),
    EventType.ORDER_REJECTED: _transition("created", "rejected", "payment_declined"),
    EventType.ORDER_CANCELLED: _transition("confirmed", "cancelled", "customer_cancelled"),
    EventType.RIDER_ASSIGNED: _transition("confirmed", "assigned"),
    EventType.ORDER_PICKED_UP: _transition("assigned", "picked_up"),
    EventType.ORDER_DELIVERED: {
        **_transition("picked_up", "delivered"),
        "items": [
            {
                "menu_item_id": "item-1",
                "name": "Chicken Biryani",
                "quantity": 2,
                "unit_price": Decimal("690.00"),
            }
        ],
    },
    EventType.PAYMENT_SUCCESSFUL: {
        "payment_id": "pay-1",
        "order_id": "ord-1",
        "customer_id": "cust-1",
        "amount": Decimal("25.50"),
        "status": "authorized",
    },
    EventType.REFUND_PROCESSED: {
        "payment_id": "pay-1",
        "order_id": "ord-1",
        "customer_id": "cust-1",
        "refund_id": "ref-1",
        "amount": Decimal("5.00"),
        "total_refunded": Decimal("5.00"),
        "reason": "cold food",
    },
    EventType.DELIVERY_STATUS_UPDATED: {
        "delivery_id": "del-1",
        "order_id": "ord-1",
        "customer_id": "cust-1",
        "rider_id": "rider-1",
        "rider_user_id": "user-rider-1",
        "from_status": "pending_assignment",
        "to_status": "offered",
        "reason": None,
    },
    EventType.DELIVERY_ETA_UPDATED: {
        "delivery_id": "del-1",
        "order_id": "ord-1",
        "customer_id": "cust-1",
        "rider_id": "rider-1",
        "status": "assigned",
        "pickup_eta": "2026-10-26T12:10:00+00:00",
        "dropoff_eta": "2026-10-26T12:25:00+00:00",
    },
    EventType.DELIVERY_FOOD_READY: {
        "delivery_id": "del-1",
        "order_id": "ord-1",
        "customer_id": "cust-1",
        "rider_id": "rider-1",
        "rider_user_id": "user-rider-1",
        "status": "assigned",
        "food_ready_at": "2026-10-26T12:05:00+00:00",
        "pickup_eta": "2026-10-26T12:10:00+00:00",
        "dropoff_eta": "2026-10-26T12:25:00+00:00",
    },
    EventType.RIDER_SHIFT_CHANGED: {
        "rider_id": "rider-1",
        "rider_user_id": "user-rider-1",
        "from_status": "offline",
        "to_status": "available",
        "on_shift": True,
        "reason": "rider",
    },
    EventType.RESTAURANT_UPSERTED: {
        "restaurant_id": "rest-1",
        "name": "Desi Dastarkhwan",
        "description": "Dum biryani and karahi in F-7.",
        "city": "Islamabad",
        "address": "Jinnah Super Market, F-7",
        "lat": 33.72,
        "lng": 73.05,
        "cuisine_type": "Pakistani",
        "status": "active",
        "is_accepting_orders": True,
        "avg_prep_time_minutes": 25,
        "avg_rating": 4.15,
        "review_count": 20,
        "source_version": 7,
    },
    EventType.RESTAURANT_STATUS_CHANGED: {
        "restaurant_id": "rest-1",
        "from_status": "pending_approval",
        "to_status": "active",
        "reason": None,
        "source_version": 2,
    },
    EventType.MENU_ITEM_UPSERTED: {
        "menu_item_id": "item-1",
        "restaurant_id": "rest-1",
        "restaurant_name": "Desi Dastarkhwan",
        "restaurant_status": "active",
        "cuisine_type": "Pakistani",
        "city": "Islamabad",
        "lat": 33.72,
        "lng": 73.05,
        "category_id": "cat-1",
        "category_name": "Rice",
        "name": "Chicken Biryani",
        "description": "Dum-cooked sela rice with bone-in chicken.",
        "price": Decimal("690.00"),
        "dietary_tags": ["halal"],
        "spice_level": 2,
        "calories": 820,
        "is_available": True,
        "in_stock": True,
        "source_version": 8,
    },
    EventType.MENU_ITEM_DELETED: {
        "menu_item_id": "item-1",
        "restaurant_id": "rest-1",
        "source_version": 9,
    },
    EventType.REVIEW_SUBMITTED: {
        "order_id": "ord-1",
        "restaurant_id": "rest-1",
        "restaurant_name": "Desi Dastarkhwan",
        "rating": 5,
        "text": "Zabardast biryani.",
        "item_ids": ["item-1"],
        "created_at": datetime(2026, 10, 15, 14, 0, tzinfo=UTC),
        "avg_rating": 4.2,
        "review_count": 21,
        "source_version": 10,
    },
}


def sample_event(event_type: EventType, event_id: str = "evt-1") -> DomainEvent:
    from sfo_events import validate_payload

    return DomainEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_id="agg-1",
        occurred_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        correlation_id="req-1",
        payload=validate_payload(event_type, SAMPLE_PAYLOADS[event_type]),
    )
