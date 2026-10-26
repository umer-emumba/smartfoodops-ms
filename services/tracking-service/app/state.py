"""Latest-known state, read from Redis keys the publishers keep current
(docs/week2-plan.md step 7):

- `tracking:order:{id}:state` — hash: the order's status and owner
  (order-service) plus delivery status, rider position and ETAs
  (delivery-service). Used for the ownership check and the on-connect
  snapshot.
- `tracking:restaurant:{id}:pending` — set of order IDs awaiting the
  restaurant's decision.
- `restaurant:{id}:owner` — the restaurant admin's user ID.

If a key is missing (Redis restarted, or expired), one call to the owning
service rebuilds it: order-service and restaurant-service re-populate the
key themselves and return the value."""

from typing import Any, cast

import httpx
from sfo_common.errors import ServiceUnavailableError
from sfo_common.middleware import propagate_correlation_id

from app import redis_client
from app.config import settings

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(3.0, connect=1.0),
    event_hooks={"request": [propagate_correlation_id]},
)


def order_state_key(order_id: str) -> str:
    return f"tracking:order:{order_id}:state"


def pending_key(restaurant_id: str) -> str:
    return f"tracking:restaurant:{restaurant_id}:pending"


def owner_key(restaurant_id: str) -> str:
    return f"restaurant:{restaurant_id}:owner"


async def _fetch(url: str) -> dict[str, Any] | None:
    try:
        resp = await _client.get(url)
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("Tracking data is temporarily unavailable") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ServiceUnavailableError("Tracking data is temporarily unavailable")
    return resp.json()


async def order_state(order_id: str) -> dict[str, Any] | None:
    """The order's tracking state, or None if there's no such order."""
    # The client decodes responses to str; the redis-py stubs can't express that.
    state = cast(dict[str, Any], await redis_client.redis.hgetall(order_state_key(order_id)))
    # Delivery-service may have written its fields alone (after the order's
    # part expired) — without the owner, the state can't be used.
    if state.get("customer_id"):
        return state
    return await _fetch(f"{settings.order_service_url}/internal/orders/{order_id}/tracking")


async def restaurant_owner(restaurant_id: str) -> str | None:
    owner = cast(str | None, await redis_client.redis.get(owner_key(restaurant_id)))
    if owner:
        return owner
    data = await _fetch(
        f"{settings.restaurant_service_url}/internal/restaurants/{restaurant_id}/owner"
    )
    return data["admin_user_id"] if data else None


async def pending_orders(restaurant_id: str) -> list[dict[str, Any]]:
    """Orders awaiting the restaurant's decision, with their state."""
    order_ids = sorted(
        cast(set[str], await redis_client.redis.smembers(pending_key(restaurant_id)))
    )
    if not order_ids:
        return []
    async with redis_client.redis.pipeline(transaction=False) as pipe:
        for order_id in order_ids:
            pipe.hgetall(order_state_key(order_id))
        states = await pipe.execute()
    return [
        {"order_id": order_id, **state}
        for order_id, state in zip(order_ids, states, strict=True)
        if state
    ]


async def close() -> None:
    await _client.aclose()
