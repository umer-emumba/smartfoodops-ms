"""Live feeds over Server-Sent Events (docs/week2-plan.md step 7, D8, D11).

- `GET /v1/tracking/orders/{order_id}` — the customer's order: a
  `snapshot` event with the latest state, then every `status`, `delivery`
  and `location` update. Ends once the order is delivered, cancelled or
  rejected.
- `GET /v1/tracking/restaurants/{restaurant_id}` — the restaurant
  dashboard: a `snapshot` of orders awaiting a decision, then `new_order`
  and `order_cancelled` events. Accept/reject goes through order-service's
  restaurant-response endpoint.

Ownership is checked before streaming starts (403/404 as normal JSON). A
`: heartbeat` comment is sent every 15s while idle. A client that falls too
far behind is disconnected — EventSource reconnects and gets a fresh
snapshot.

Browsers' EventSource can't send headers: pass the access token as
`?access_token=` — api-gateway moves it into the Authorization header."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from prometheus_client import Gauge
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ForbiddenError, NotFoundError

from app import sse, state
from app.auth_deps import get_current_user
from app.config import settings
from app.hub import hub

ACTIVE_STREAMS = Gauge("tracking_active_streams", "Open live-tracking streams", ["feed"])

router = APIRouter(prefix="/v1/tracking", tags=["tracking"])

TERMINAL = {"delivered", "cancelled", "rejected"}


def order_channel(order_id: str) -> str:
    return f"tracking:order:{order_id}"


def restaurant_channel(restaurant_id: str) -> str:
    return f"tracking:restaurant:{restaurant_id}"


def _is_terminal(message: dict[str, Any]) -> bool:
    return message.get("type") == "status" and message.get("status") in TERMINAL


async def _stream(
    channel: str,
    snapshot: Callable[[], Awaitable[dict[str, Any]]],
    *,
    ends: Callable[[dict[str, Any]], bool],
) -> AsyncIterator[str]:
    """Subscribes *before* reading the snapshot, so nothing published in
    between is missed (at worst a message repeats what the snapshot showed).
    Unsubscribes however the stream ends — including the client
    disconnecting, which cancels this generator."""
    subscription = await hub.subscribe(channel)
    feed = channel.split(":")[1]  # tracking:order:{id} / tracking:restaurant:{id}
    ACTIVE_STREAMS.labels(feed).inc()
    try:
        first = await snapshot()
        yield sse.event("snapshot", first)
        if ends(first):
            return
        while not subscription.overflowed:
            try:
                raw = await asyncio.wait_for(
                    subscription.queue.get(), timeout=settings.heartbeat_seconds
                )
            except TimeoutError:
                yield sse.HEARTBEAT
                continue
            message = json.loads(raw)
            yield sse.event(message.get("type", "message"), message)
            if ends(message):
                return
    finally:
        ACTIVE_STREAMS.labels(feed).dec()
        await hub.unsubscribe(subscription)


def _sse(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=sse.HEADERS)


@router.get("/orders/{order_id}")
async def track_order(
    order_id: str, current: CurrentUser = Depends(get_current_user)
) -> StreamingResponse:
    """Live tracking for the ordering customer (or a system_admin)."""
    current_state = await state.order_state(order_id)
    if current_state is None:
        raise NotFoundError("Order not found")
    if current.role != Role.SYSTEM_ADMIN and current.id != current_state.get("customer_id"):
        raise ForbiddenError("Not your order")

    async def snapshot() -> dict[str, Any]:
        return {"type": "snapshot", **(await state.order_state(order_id) or current_state)}

    return _sse(
        _stream(
            order_channel(order_id),
            snapshot,
            ends=lambda m: (
                m.get("status") in TERMINAL if m.get("type") == "snapshot" else _is_terminal(m)
            ),
        )
    )


@router.get("/restaurants/{restaurant_id}")
async def restaurant_feed(
    restaurant_id: str, current: CurrentUser = Depends(get_current_user)
) -> StreamingResponse:
    """New orders and cancellations for the owning restaurant admin (or a
    system_admin)."""
    owner = await state.restaurant_owner(restaurant_id)
    if owner is None:
        raise NotFoundError("Restaurant not found")
    if current.role != Role.SYSTEM_ADMIN and current.id != owner:
        raise ForbiddenError("Not your restaurant")

    async def snapshot() -> dict[str, Any]:
        return {"type": "snapshot", "pending": await state.pending_orders(restaurant_id)}

    return _sse(_stream(restaurant_channel(restaurant_id), snapshot, ends=lambda _: False))
