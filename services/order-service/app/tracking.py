"""Live-feed state written by order-service itself (docs/week2-plan.md
step 7, D8): `tracking:order:{id}:state` (hash) with the order's status and
owners — tracking-service's ownership check and on-connect snapshot — and
`tracking:restaurant:{id}:pending` (set) while the order awaits the
restaurant's decision.

Only for a new order (trackable at once, before its first event reaches
Kafka) and for rebuilding a missing key (GET /internal/orders/{id}/tracking).
Every status change after that reaches the live feeds through its Kafka event:
tracking-service applies it and publishes it (its app/projection.py), so an
update can't be lost to a Redis outage here. Best-effort: the order row
stays the source of truth."""

import logging
from datetime import UTC, datetime

from redis.exceptions import RedisError

from app import redis_client
from app.models import Order, OrderStatus

_logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 24 * 3600
FINISHED_STATE_TTL_SECONDS = 3600
_FINISHED = {OrderStatus.DELIVERED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


def order_channel(order_id: str) -> str:
    return f"tracking:order:{order_id}"


def restaurant_channel(restaurant_id: str) -> str:
    return f"tracking:restaurant:{restaurant_id}"


def state_key(order_id: str) -> str:
    return f"tracking:order:{order_id}:state"


def pending_key(restaurant_id: str) -> str:
    return f"tracking:restaurant:{restaurant_id}:pending"


def state_fields(order: Order) -> dict[str, str | int]:
    """The order's part of the state hash (delivery-service writes its own
    fields alongside). Redis hashes hold strings: None becomes ""."""
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "customer_id": order.customer_id,
        "restaurant_id": order.restaurant_id,
        "restaurant_admin_id": order.restaurant_admin_id or "",
        "status": order.status,
        "status_reason": order.status_reason or "",
        "total_amount": str(order.total_amount),
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def _write(order: Order) -> dict[str, str | int]:
    fields = state_fields(order)
    key = state_key(order.id)
    async with redis_client.redis.pipeline(transaction=False) as pipe:
        # The stubs' Mapping key type is invariant; any str-keyed dict is rejected.
        pipe.hset(key, mapping=fields)  # type: ignore[arg-type]
        pipe.expire(
            key, FINISHED_STATE_TTL_SECONDS if order.status in _FINISHED else STATE_TTL_SECONDS
        )
        if order.status == OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE:
            pipe.sadd(pending_key(order.restaurant_id), order.id)
        else:
            pipe.srem(pending_key(order.restaurant_id), order.id)
        await pipe.execute()
    return fields


async def write_state(order: Order) -> dict[str, str | int]:
    """State only (new order; rebuilding a missing key). Returns it."""
    fields = state_fields(order)
    try:
        fields = await _write(order)
    except RedisError:
        _logger.warning(
            "Tracking state write failed",
            exc_info=True,
            extra={"extra_fields": {"order_id": order.id}},
        )
    return fields
