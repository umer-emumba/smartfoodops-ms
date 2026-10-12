"""Live-tracking publishes to `tracking:order:{order_id}` (docs/week2-plan.md
D8): delivery status changes and, while a rider has the order, the rider's
position. tracking-service (step 7) relays the channel to the customer.
Best-effort, after commit — the database stays the source of truth."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError

from app import redis_client
from app.models import Delivery

_logger = logging.getLogger(__name__)


def order_channel(order_id: str) -> str:
    return f"tracking:order:{order_id}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _publish(order_id: str, message: dict[str, Any]) -> None:
    message["at"] = datetime.now(UTC).isoformat()
    try:
        await redis_client.redis.publish(order_channel(order_id), json.dumps(message))
    except RedisError:
        _logger.warning(
            "Tracking publish failed", exc_info=True, extra={"extra_fields": {"order_id": order_id}}
        )


async def publish_delivery(delivery: Delivery) -> None:
    await _publish(
        delivery.order_id,
        {
            "type": "delivery",
            "delivery_id": delivery.id,
            "status": delivery.status,
            "rider_id": delivery.rider_id,
            "pickup_eta": _iso(delivery.pickup_eta),
            "dropoff_eta": _iso(delivery.dropoff_eta),
        },
    )


async def publish_location(order_id: str, rider_id: str, lat: float, lng: float) -> None:
    await _publish(order_id, {"type": "location", "rider_id": rider_id, "lat": lat, "lng": lng})
