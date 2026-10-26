"""Domain events shaped like the real producers' output."""

from datetime import UTC, datetime
from typing import Any

from sfo_common import new_id
from sfo_events import DomainEvent, EventType, validate_payload

CUSTOMER, OWNER, RIDER_USER = "u-customer", "u-owner", "u-rider"


def order_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "order_id": "o-1",
        "order_number": 1042,
        "customer_id": CUSTOMER,
        "restaurant_id": "r-1",
        "restaurant_admin_id": OWNER,
        "total_amount": "1250.00",
        "from_status": "created",
        "to_status": "x",
        "reason": None,
        **overrides,
    }


def delivery_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "delivery_id": "d-1",
        "order_id": "o-1",
        "customer_id": CUSTOMER,
        "rider_id": "rider-1",
        "rider_user_id": RIDER_USER,
        "from_status": "pending_assignment",
        "to_status": "offered",
        "reason": None,
        **overrides,
    }


def event(
    event_type: EventType, payload: dict[str, Any], event_id: str | None = None
) -> DomainEvent:
    return DomainEvent(
        event_id=event_id or new_id(),
        event_type=event_type,
        aggregate_id=payload.get("order_id") or payload.get("rider_id") or "agg",
        occurred_at=datetime.now(UTC),
        payload=validate_payload(event_type, payload),
    )


def placed() -> DomainEvent:
    payload = order_payload(items=[{"menu_item_id": "m-1", "quantity": 1}])
    for key in ("from_status", "to_status", "reason"):
        payload.pop(key)
    return event(EventType.ORDER_PLACED, payload)
