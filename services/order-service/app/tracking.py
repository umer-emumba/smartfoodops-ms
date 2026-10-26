"""Live-feed state and publishes (docs/week2-plan.md step 7, D8, D11).

On every status change (and at creation):
- `tracking:order:{id}:state` (hash) gets the order's status and owners —
  tracking-service's ownership check and on-connect snapshot. Kept 24h, and
  1h once the order is finished;
- `tracking:restaurant:{id}:pending` (set) holds the order while it awaits
  the restaurant's decision — the restaurant feed's snapshot;
- the change is published to `tracking:order:{id}`, and a new order or a
  cancellation also to `tracking:restaurant:{id}`.

Best-effort and after commit: a missed write is only a late UI update — the
order row stays the source of truth, and tracking-service rebuilds a missing
state key from GET /internal/orders/{id}/tracking."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

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


def _messages(order: Order, at: str) -> list[tuple[str, dict[str, Any]]]:
    messages: list[tuple[str, dict[str, Any]]] = [
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
                    "total_amount": str(order.total_amount),
                    "at": at,
                },
            )
        )
    return messages


async def _write(order: Order, *, publish: bool) -> dict[str, str | int]:
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
        if publish:
            for channel, message in _messages(order, str(fields["updated_at"])):
                pipe.publish(channel, json.dumps(message))
        await pipe.execute()
    return fields


async def publish_status(order: Order) -> None:
    """After a status change: state + publish."""
    try:
        await _write(order, publish=True)
    except RedisError:
        _logger.warning(
            "Tracking publish failed",
            exc_info=True,
            extra={"extra_fields": {"order_id": order.id, "status": order.status}},
        )


async def write_state(order: Order) -> dict[str, str | int]:
    """State only (new order; rebuilding a missing key). Returns it."""
    fields = state_fields(order)
    try:
        fields = await _write(order, publish=False)
    except RedisError:
        _logger.warning(
            "Tracking state write failed",
            exc_info=True,
            extra={"extra_fields": {"order_id": order.id}},
        )
    return fields
