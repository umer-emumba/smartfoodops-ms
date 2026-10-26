"""Live tracking (docs/week2-plan.md D8): while a rider has the order, the
rider's position is written into the order's state hash
`tracking:order:{order_id}:state` (the snapshot a customer gets on
connecting) and published to `tracking:order:{order_id}`, which
tracking-service relays.

Only positions: frequent, and fine to lose one, so straight to Redis,
best-effort. Delivery status and ETAs reach the live feeds through their
Kafka events instead (tracking-service's app/projection.py)."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError

from app import redis_client

_logger = logging.getLogger(__name__)


def order_channel(order_id: str) -> str:
    return f"tracking:order:{order_id}"


def state_key(order_id: str) -> str:
    return f"tracking:order:{order_id}:state"


# Matches order-service's: if this service writes first (or after the
# order's part expired), the key still expires.
_STATE_TTL_SECONDS = 24 * 3600


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


async def publish_location(order_id: str, rider_id: str, lat: float, lng: float) -> None:
    await _publish(
        order_id,
        {"type": "location", "rider_id": rider_id, "lat": lat, "lng": lng},
        {"rider_lat": lat, "rider_lng": lng, "rider_location_at": datetime.now(UTC).isoformat()},
    )
