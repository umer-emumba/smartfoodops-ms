"""Live-feed publishes (docs/week2-plan.md D8, D11). Every status change goes
to the customer's channel `tracking:order:{id}`; a new order reaching the
restaurant, and a cancellation, also go to `tracking:restaurant:{id}`.
tracking-service (step 7) relays these channels to SSE clients.

Best-effort and after commit: Pub/Sub has no history, so a missed message is
only a late UI update — the order row stays the source of truth (step 7 adds
an on-connect snapshot from a Redis state key)."""

import json
import logging
from datetime import UTC, datetime

from redis.exceptions import RedisError

from app.models import Order, OrderStatus
from app.redis_client import redis

_logger = logging.getLogger(__name__)


def order_channel(order_id: str) -> str:
    return f"tracking:order:{order_id}"


def restaurant_channel(restaurant_id: str) -> str:
    return f"tracking:restaurant:{restaurant_id}"


async def publish_status(order: Order) -> None:
    at = datetime.now(UTC).isoformat()
    messages: list[tuple[str, dict[str, object]]] = [
        (
            order_channel(order.id),
            {
                "type": "status",
                "order_id": order.id,
                "status": order.status,
                "reason": order.status_reason,
                "at": at,
            },
        )
    ]
    if order.status in (OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE, OrderStatus.CANCELLED):
        messages.append(
            (
                restaurant_channel(order.restaurant_id),
                {
                    "type": "new_order"
                    if order.status == OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE
                    else "order_cancelled",
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "at": at,
                },
            )
        )
    try:
        for channel, message in messages:
            await redis.publish(channel, json.dumps(message))
    except RedisError:
        _logger.warning(
            "Tracking publish failed",
            exc_info=True,
            extra={"extra_fields": {"order_id": order.id, "status": order.status}},
        )
