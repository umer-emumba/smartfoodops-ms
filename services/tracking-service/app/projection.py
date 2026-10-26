"""Applies order and delivery events from Kafka (order-events,
delivery-events; app/consumer.py) to the live feeds:

- `tracking:order:{id}:state` (hash) gets the order's status and owners
  (order events) and its delivery status, rider and ETAs (delivery events)
  — the ownership check and on-connect snapshot (app/state.py). Kept 24h,
  and 1h once the order is finished;
- `tracking:restaurant:{id}:pending` (set) holds the order while it awaits
  the restaurant's decision — the restaurant feed's snapshot;
- `tracking:rider:{user_id}:state` (hash) holds the delivery a rider
  currently has (offered or held) — the rider feed's snapshot;
- each change is published to `tracking:order:{id}`, a new order or a
  cancellation also to `tracking:restaurant:{id}`, and a rider's offers and
  deliveries to `tracking:rider:{user_id}`, for the open streams (app/hub.py).

Driven by Kafka, not published by order- and delivery-service themselves,
so an update can't be lost: while Redis is down the consumer waits and
retries, and it picks up whatever it missed
(docs/architecture.md#real-time-delivery-tracking).

Each event is applied by one Lua script — the check, the writes and the
publishes together: atomic against order-service's rebuilds, and
all-or-nothing if Redis fails mid-way, so a retry never finds the state
written but the update unpublished. Stale events change and publish nothing:

- Orders never move backwards (order-service's app/state_machine.py), so an
  order event for a status no later than the stored one — a duplicate, a
  replay, or one older than a rebuilt state — is skipped.
- A delivery can go back (a declined offer returns it to
  `pending_assignment`), so delivery events are compared by time instead:
  all of one delivery's events are on one partition, in order, so a
  delivery event no newer than the last one applied is a redelivery.
- A rider's state is cleared only by an event about the delivery it holds:
  events about two deliveries sit on different partitions and may arrive in
  either order, and a late "released" for an old offer mustn't wipe a new one.

Events older than a state's lifetime (24h) are skipped: their state would
already have expired. So a new consumer group can start at the oldest events
(a week of them) without filling Redis with long-finished orders, and no
event is ever skipped for being "before the group existed".

Rider positions are still written and published by delivery-service
directly, best-effort: they arrive every few seconds."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sfo_events import DomainEvent
from sfo_events.payloads import (
    DeliveryEtaUpdatedPayload,
    DeliveryStatusUpdatedPayload,
    OrderPlacedPayload,
    OrderTransitionPayload,
)

from app import state

STATE_TTL_SECONDS = 24 * 3600
FINISHED_STATE_TTL_SECONDS = 3600
AWAITING_RESTAURANT = "awaiting_restaurant_acceptance"
FINISHED = frozenset({"delivered", "rejected", "cancelled"})
# Position in the lifecycle. The finished statuses share the last place:
# an order reaches only one of them.
STATUS_RANK = {
    "created": 0,
    AWAITING_RESTAURANT: 1,
    "confirmed": 2,
    "assigned": 3,
    "picked_up": 4,
    "delivered": 5,
    "rejected": 5,
    "cancelled": 5,
}

# KEYS: state hash, restaurant's pending set.
# ARGV: status ranks (JSON), new status, TTL, order ID, "1" if awaiting the
# restaurant, messages (JSON list of [channel, message]), then field, value...
# Returns 1 if applied, 0 if the stored status is the same or later.
_APPLY_ORDER = """
local rank = cjson.decode(ARGV[1])
local current = redis.call('HGET', KEYS[1], 'status')
if current and rank[current] and rank[current] >= rank[ARGV[2]] then
  return 0
end
redis.call('HSET', KEYS[1], unpack(ARGV, 7))
redis.call('EXPIRE', KEYS[1], ARGV[3])
if ARGV[5] == '1' then
  redis.call('SADD', KEYS[2], ARGV[4])
else
  redis.call('SREM', KEYS[2], ARGV[4])
end
for _, message in ipairs(cjson.decode(ARGV[6])) do
  redis.call('PUBLISH', message[1], message[2])
end
return 1
"""
_RANKS_JSON = json.dumps(STATUS_RANK)

# KEYS: order state hash, rider state hash (unused when no rider is involved).
# ARGV: event time (µs), rider action ("set" / "clear" / "none"), delivery ID,
# rider fields (JSON object, for "set"), messages (JSON list of [channel,
# message]), then field, value... for the order state.
# Returns 1 if applied, 0 if a newer or equal event was already applied.
_APPLY_DELIVERY = """
local last = tonumber(redis.call('HGET', KEYS[1], 'delivery_event_us') or '0')
if last >= tonumber(ARGV[1]) then
  return 0
end
redis.call('HSET', KEYS[1], 'delivery_event_us', ARGV[1], unpack(ARGV, 6))
-- NX: never shortens or extends the order part's own expiry.
redis.call('EXPIRE', KEYS[1], 86400, 'NX')
if ARGV[2] == 'set' then
  redis.call('DEL', KEYS[2])
  for field, value in pairs(cjson.decode(ARGV[4])) do
    redis.call('HSET', KEYS[2], field, value)
  end
  redis.call('EXPIRE', KEYS[2], 86400)
elseif ARGV[2] == 'clear' and redis.call('HGET', KEYS[2], 'delivery_id') == ARGV[3] then
  redis.call('DEL', KEYS[2])
end
for _, message in ipairs(cjson.decode(ARGV[5])) do
  redis.call('PUBLISH', message[1], message[2])
end
return 1
"""

type OrderPayload = OrderPlacedPayload | OrderTransitionPayload


def _too_old(event: DomainEvent) -> bool:
    return datetime.now(UTC) - event.occurred_at > timedelta(seconds=STATE_TTL_SECONDS)


async def _eval(redis: Redis, script: str, *args: Any) -> bool:
    """A Redis outage is raised as ConnectionError, which the consumer loop
    waits out instead of dead-lettering the event
    (sfo_events.consumer.is_transient)."""
    try:
        applied = await redis.eval(script, *args)  # type: ignore[misc]  # stubs: not awaitable
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ConnectionError("Redis is unreachable") from exc
    return bool(applied)


def _pairs(fields: dict[str, Any]) -> list[str]:
    """field, value, ... for HSET. Redis hashes hold strings: None becomes ""."""
    return [item for f, v in fields.items() for item in (f, "" if v is None else str(v))]


async def apply_event(redis: Redis, event: DomainEvent) -> bool:
    """True if the event changed a live feed (state written, update
    published); False for a stale, too old or unrelated event."""
    if _too_old(event):
        return False
    p = event.typed_payload()
    if isinstance(p, OrderPlacedPayload | OrderTransitionPayload):
        return await apply_order_event(redis, event)
    if isinstance(p, DeliveryStatusUpdatedPayload | DeliveryEtaUpdatedPayload):
        return await apply_delivery_event(redis, event)
    return False


# ---------- Orders ----------


def _order_messages(p: OrderPayload, status: str, reason: str | None, at: str) -> list[list[str]]:
    messages = [
        [
            state.order_channel(p.order_id),
            json.dumps(
                {
                    "type": "status",
                    "order_id": p.order_id,
                    "status": status,
                    "reason": reason,
                    "at": at,
                }
            ),
        ]
    ]
    if status in (AWAITING_RESTAURANT, "cancelled"):
        messages.append(
            [
                state.restaurant_channel(p.restaurant_id),
                json.dumps(
                    {
                        "type": "new_order" if status == AWAITING_RESTAURANT else "order_cancelled",
                        "order_id": p.order_id,
                        "order_number": p.order_number,
                        "total_amount": str(p.total_amount),
                        "at": at,
                    }
                ),
            ]
        )
    return messages


async def apply_order_event(redis: Redis, event: DomainEvent) -> bool:
    """An OrderPlaced or order status event; True if it moved the order
    forward."""
    if _too_old(event):
        return False
    p = event.typed_payload()
    if isinstance(p, OrderPlacedPayload):
        status, reason = "created", None
    elif isinstance(p, OrderTransitionPayload):
        status, reason = p.to_status, p.reason
    else:
        return False
    if status not in STATUS_RANK:
        return False
    at = event.occurred_at.isoformat()
    fields = {
        "order_id": p.order_id,
        "order_number": p.order_number,
        "customer_id": p.customer_id,
        "restaurant_id": p.restaurant_id,
        "restaurant_admin_id": p.restaurant_admin_id,
        "status": status,
        "status_reason": reason,
        "total_amount": p.total_amount,
        "updated_at": at,
    }
    return await _eval(
        redis,
        _APPLY_ORDER,
        2,
        state.order_state_key(p.order_id),
        state.pending_key(p.restaurant_id),
        _RANKS_JSON,
        status,
        FINISHED_STATE_TTL_SECONDS if status in FINISHED else STATE_TTL_SECONDS,
        p.order_id,
        "1" if status == AWAITING_RESTAURANT else "0",
        json.dumps(_order_messages(p, status, reason, at)),
        *_pairs(fields),
    )


# ---------- Deliveries ----------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _rider_update(
    p: DeliveryStatusUpdatedPayload, at: str
) -> tuple[str | None, str, dict[str, Any] | None]:
    """(rider to tell, "set"/"clear"/"none", their message) for a status
    change. The rider app treats every message as a nudge and fetches the
    details over REST (GET /v1/riders/me/offer, /v1/deliveries/...)."""
    base = {"delivery_id": p.delivery_id, "order_id": p.order_id, "at": at}
    if p.to_status == "pending_assignment" and p.released_rider_user_id:
        # Their offer was declined or expired, or they dropped out.
        return (
            p.released_rider_user_id,
            "clear",
            {"type": "released", "reason": p.reason, **base},
        )
    if p.rider_user_id is None:
        return None, "none", None
    if p.to_status == "offered":
        return p.rider_user_id, "set", {"type": "offer", **base}
    if p.to_status == "cancelled":
        return p.rider_user_id, "clear", {"type": "released", "reason": p.reason, **base}
    if p.to_status in ("assigned", "picked_up"):
        return p.rider_user_id, "set", {"type": "delivery", "status": p.to_status, **base}
    if p.to_status == "delivered":
        return p.rider_user_id, "clear", {"type": "delivery", "status": p.to_status, **base}
    return None, "none", None


async def apply_delivery_event(redis: Redis, event: DomainEvent) -> bool:
    """A delivery status or ETA event: the customer's delivery details, and
    the rider's feed for offers and the delivery they hold. True if applied."""
    if _too_old(event):
        return False
    p = event.typed_payload()
    if isinstance(p, DeliveryStatusUpdatedPayload):
        status = p.to_status
        rider, action, rider_message = _rider_update(p, event.occurred_at.isoformat())
    elif isinstance(p, DeliveryEtaUpdatedPayload):
        status, rider, action, rider_message = p.status, None, "none", None
    else:
        return False
    at = event.occurred_at.isoformat()
    pickup_eta, dropoff_eta = _iso(p.pickup_eta), _iso(p.dropoff_eta)
    messages = [
        [
            state.order_channel(p.order_id),
            json.dumps(
                {
                    "type": "delivery",
                    "delivery_id": p.delivery_id,
                    "status": status,
                    "rider_id": p.rider_id,
                    "pickup_eta": pickup_eta,
                    "dropoff_eta": dropoff_eta,
                    "at": at,
                }
            ),
        ]
    ]
    if rider is not None and rider_message is not None:
        messages.append([state.rider_channel(rider), json.dumps(rider_message)])
    rider_fields = {
        "delivery_id": p.delivery_id,
        "order_id": p.order_id,
        "status": status,
        "updated_at": at,
    }
    order_fields = {
        "delivery_id": p.delivery_id,
        "delivery_status": status,
        "rider_id": p.rider_id,
        "pickup_eta": pickup_eta,
        "dropoff_eta": dropoff_eta,
    }
    return await _eval(
        redis,
        _APPLY_DELIVERY,
        2,
        state.order_state_key(p.order_id),
        # Always a key (Redis scripts declare theirs); untouched for "none".
        state.rider_state_key(rider or "-"),
        int(event.occurred_at.timestamp() * 1_000_000),
        action,
        p.delivery_id,
        json.dumps(rider_fields),
        json.dumps(messages),
        *_pairs(order_fields),
    )
