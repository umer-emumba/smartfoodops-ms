"""Applies order events from Kafka (order-events, app/consumer.py) to the
live feeds:

- `tracking:order:{id}:state` (hash) gets the order's status and owners —
  the ownership check and on-connect snapshot (app/state.py). Kept 24h, and
  1h once the order is finished;
- `tracking:restaurant:{id}:pending` (set) holds the order while it awaits
  the restaurant's decision — the restaurant feed's snapshot;
- the change is published to `tracking:order:{id}`, and a new order or a
  cancellation also to `tracking:restaurant:{id}`, for the open streams
  (app/hub.py).

Driven by Kafka, not published by order-service itself, so a status update
can't be lost: while Redis is down the consumer waits and retries, and it
picks up whatever it missed (docs/architecture.md#real-time-delivery-tracking).

Never backwards: an order only moves forward through its statuses
(order-service's app/state_machine.py), so an event for a status no later
than the stored one — a duplicate, a replay, or one older than a state
order-service rebuilt from its database — changes and publishes nothing.
The check, the writes and the publishes are one Lua script: atomic against
those rebuilds, and all-or-nothing if Redis fails mid-way, so a retry never
finds the state written but the update unpublished.

Events older than a state's lifetime (24h) are skipped: their state would
already have expired. So a new consumer group can start at the oldest events
(a week of them) without filling Redis with long-finished orders, and no
event is ever skipped for being "before the group existed".

Delivery details (delivery status, ETAs, rider position) are still written
and published by delivery-service directly, best-effort: the order's own
progress — assigned, picked up, delivered — comes through here."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sfo_events import DomainEvent
from sfo_events.payloads import OrderPlacedPayload, OrderTransitionPayload

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
_APPLY = """
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

type OrderPayload = OrderPlacedPayload | OrderTransitionPayload


def _messages(p: OrderPayload, status: str, reason: str | None, at: str) -> list[list[str]]:
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
    """True if the event moved the order forward (state written, update
    published); False for a stale, too old or non-order event. A Redis outage is
    raised as ConnectionError, which the consumer loop waits out instead of
    dead-lettering the event (sfo_events.consumer.is_transient)."""
    if datetime.now(UTC) - event.occurred_at > timedelta(seconds=STATE_TTL_SECONDS):
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
    # Redis hashes hold strings: None becomes "".
    fields: dict[str, Any] = {
        "order_id": p.order_id,
        "order_number": p.order_number,
        "customer_id": p.customer_id,
        "restaurant_id": p.restaurant_id,
        "restaurant_admin_id": p.restaurant_admin_id or "",
        "status": status,
        "status_reason": reason or "",
        "total_amount": str(p.total_amount),
        "updated_at": at,
    }
    try:
        applied = await redis.eval(  # type: ignore[misc]  # stubs: eval isn't awaitable
            _APPLY,
            2,
            state.order_state_key(p.order_id),
            state.pending_key(p.restaurant_id),
            _RANKS_JSON,
            status,
            FINISHED_STATE_TTL_SECONDS if status in FINISHED else STATE_TTL_SECONDS,
            p.order_id,
            "1" if status == AWAITING_RESTAURANT else "0",
            json.dumps(_messages(p, status, reason, at)),
            *(item for field, value in fields.items() for item in (field, str(value))),
        )
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ConnectionError("Redis is unreachable") from exc
    return bool(applied)
