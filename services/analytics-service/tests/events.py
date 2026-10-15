"""Domain events shaped like the real producers' output, at chosen times."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sfo_common import new_id
from sfo_events import DomainEvent, EventType, validate_payload

T0 = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)  # 12:00 in Asia/Karachi


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def event(
    event_type: EventType, payload: dict[str, Any], minute: float, event_id: str | None = None
) -> DomainEvent:
    return DomainEvent(
        event_id=event_id or new_id(),
        event_type=event_type,
        aggregate_id=payload.get("order_id") or payload.get("rider_id") or "agg",
        occurred_at=at(minute),
        payload=validate_payload(event_type, payload),
    )


def order(
    event_type: EventType,
    minute: float,
    *,
    order_id: str = "o-1",
    restaurant_id: str = "r-1",
    reason: str | None = None,
    from_status: str = "created",
) -> DomainEvent:
    payload: dict[str, Any] = {
        "order_id": order_id,
        "order_number": 1000,
        "customer_id": "u-customer",
        "restaurant_id": restaurant_id,
        "restaurant_admin_id": "u-owner",
        "total_amount": "1250.00",
    }
    if event_type == EventType.ORDER_PLACED:
        payload["items"] = [{"menu_item_id": "m-1", "quantity": 1}]
    else:
        payload |= {"from_status": from_status, "to_status": "x", "reason": reason}
    return event(event_type, payload, minute)


def delivery(
    to_status: str,
    minute: float,
    *,
    order_id: str = "o-1",
    rider_id: str | None = "rider-1",
    reason: str | None = None,
) -> DomainEvent:
    return event(
        EventType.DELIVERY_STATUS_UPDATED,
        {
            "delivery_id": f"d-{order_id}",
            "order_id": order_id,
            "customer_id": "u-customer",
            "rider_id": rider_id,
            "rider_user_id": f"user-{rider_id}" if rider_id else None,
            "from_status": None,
            "to_status": to_status,
            "reason": reason,
        },
        minute,
    )


def shift(rider_id: str, on_shift: bool, minute: float) -> DomainEvent:
    return event(
        EventType.RIDER_SHIFT_CHANGED,
        {
            "rider_id": rider_id,
            "rider_user_id": f"user-{rider_id}",
            "from_status": "offline" if on_shift else "available",
            "to_status": "available" if on_shift else "offline",
            "on_shift": on_shift,
            "reason": "rider",
        },
        minute,
    )
