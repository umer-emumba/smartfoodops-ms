"""Live tracking (docs/week2-plan.md D8): delivery status changes and, while
a rider has the order, the rider's position are written into the order's
state hash `tracking:order:{order_id}:state` (next to order-service's
fields — the snapshot a customer gets on connecting) and published to
`tracking:order:{order_id}`, which tracking-service relays.
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


def state_key(order_id: str) -> str:
    return f"tracking:order:{order_id}:state"


# Matches order-service's: if this service writes first (or after the
# order's part expired), the key still expires.
_STATE_TTL_SECONDS = 24 * 3600


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _publish(order_id: str, message: dict[str, Any], state: dict[str, Any]) -> None:
    message["at"] = datetime.now(UTC).isoformat()
    key = state_key(order_id)
    try:
        async with redis_client.redis.pipeline(transaction=False) as pipe:
            # Redis hashes hold strings: None becomes "".
            pipe.hset(key, mapping={k: "" if v is None else str(v) for k, v in state.items()})
            # NX: never shortens or extends order-service's own expiry.
            pipe.expire(key, _STATE_TTL_SECONDS, nx=True)
            pipe.publish(order_channel(order_id), json.dumps(message))
            await pipe.execute()
    except RedisError:
        _logger.warning(
            "Tracking publish failed", exc_info=True, extra={"extra_fields": {"order_id": order_id}}
        )


async def publish_delivery(delivery: Delivery) -> None:
    pickup_eta, dropoff_eta = _iso(delivery.pickup_eta), _iso(delivery.dropoff_eta)
    await _publish(
        delivery.order_id,
        {
            "type": "delivery",
            "delivery_id": delivery.id,
            "status": delivery.status,
            "rider_id": delivery.rider_id,
            "pickup_eta": pickup_eta,
            "dropoff_eta": dropoff_eta,
        },
        {
            "delivery_id": delivery.id,
            "delivery_status": delivery.status,
            "rider_id": delivery.rider_id,
            "pickup_eta": pickup_eta,
            "dropoff_eta": dropoff_eta,
        },
    )


async def publish_location(order_id: str, rider_id: str, lat: float, lng: float) -> None:
    await _publish(
        order_id,
        {"type": "location", "rider_id": rider_id, "lat": lat, "lng": lng},
        {"rider_lat": lat, "rider_lng": lng, "rider_location_at": datetime.now(UTC).isoformat()},
    )
