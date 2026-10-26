"""Order events from Kafka → live-feed state and publishes (app/projection.py),
against Redis."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app import projection
from redis.asyncio import Redis, from_url
from redis.asyncio.client import PubSub
from sfo_events import DomainEvent
from sfo_events.consumer import is_transient

from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.db

ORDER, RESTAURANT = "o-1", "r-1"
STATE = f"tracking:order:{ORDER}:state"
PENDING = f"tracking:restaurant:{RESTAURANT}:pending"
ORDER_CHANNEL, RESTAURANT_CHANNEL = f"tracking:order:{ORDER}", f"tracking:restaurant:{RESTAURANT}"

PLACED_AT = datetime.now(UTC) - timedelta(minutes=10)
MOVED_AT = datetime.now(UTC) - timedelta(minutes=5)
_ORDER_FIELDS = {
    "order_id": ORDER,
    "order_number": 1042,
    "customer_id": "u-customer",
    "restaurant_id": RESTAURANT,
    "restaurant_admin_id": "u-owner",
    "total_amount": "12.50",
}


def _placed() -> DomainEvent:
    return DomainEvent(
        event_id="e-placed",
        event_type="OrderPlaced",
        aggregate_id=ORDER,
        occurred_at=PLACED_AT,
        payload={**_ORDER_FIELDS, "items": [{"menu_item_id": "m-1", "quantity": 1}]},
    )


def _moved(
    event_type: str, from_status: str, to_status: str, reason: str | None = None
) -> DomainEvent:
    return DomainEvent(
        event_id=f"e-{to_status}",
        event_type=event_type,
        aggregate_id=ORDER,
        occurred_at=MOVED_AT,
        payload={
            **_ORDER_FIELDS,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
        },
    )


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def published(redis: Redis) -> AsyncIterator[PubSub]:
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(ORDER_CHANNEL, RESTAURANT_CHANNEL)
    yield pubsub
    await pubsub.aclose()


async def _drain(pubsub: PubSub) -> list[tuple[str, dict[str, Any]]]:
    """Everything published so far, as (channel, message)."""
    received = []
    # Reads for a short window: a None can also be a skipped subscribe reply.
    deadline = asyncio.get_running_loop().time() + 0.5
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(timeout=0.05)
        if message is not None:
            received.append((message["channel"], json.loads(message["data"])))
    return received


async def test_order_moves_forward_through_state_pending_and_both_feeds(
    redis: Redis, published: PubSub
) -> None:
    assert await projection.apply_order_event(redis, _placed())
    assert (await redis.hgetall(STATE))["status"] == "created"

    awaiting = _moved("OrderAwaitingRestaurant", "created", "awaiting_restaurant_acceptance")
    assert await projection.apply_order_event(redis, awaiting)
    state = await redis.hgetall(STATE)
    assert (state["status"], state["customer_id"], state["order_number"]) == (
        "awaiting_restaurant_acceptance",
        "u-customer",
        "1042",
    )
    assert state["updated_at"] == MOVED_AT.isoformat()  # when it happened
    assert await redis.smembers(PENDING) == {ORDER}
    assert 23 * 3600 < await redis.ttl(STATE) <= 24 * 3600

    messages = await _drain(published)
    assert [(channel, m["type"]) for channel, m in messages] == [
        (ORDER_CHANNEL, "status"),
        (ORDER_CHANNEL, "status"),
        (RESTAURANT_CHANNEL, "new_order"),
    ]
    assert messages[2][1] | {"at": None} == {
        "type": "new_order",
        "order_id": ORDER,
        "order_number": 1042,
        "total_amount": "12.50",
        "at": None,
    }


async def test_duplicate_or_older_event_changes_and_publishes_nothing(
    redis: Redis, published: PubSub
) -> None:
    confirmed = _moved("OrderConfirmed", "awaiting_restaurant_acceptance", "confirmed")
    assert await projection.apply_order_event(redis, confirmed)
    await _drain(published)

    assert not await projection.apply_order_event(redis, confirmed)  # redelivered
    assert not await projection.apply_order_event(redis, _placed())  # older
    assert (await redis.hgetall(STATE))["status"] == "confirmed"
    assert await _drain(published) == []


async def test_event_older_than_a_rebuilt_state_is_ignored(redis: Redis, published: PubSub) -> None:
    # order-service rebuilt the key from its database: already picked up.
    await redis.hset(STATE, mapping={**_ORDER_FIELDS, "status": "picked_up"})
    assigned = _moved("RiderAssigned", "confirmed", "assigned")
    assert not await projection.apply_order_event(redis, assigned)
    assert (await redis.hgetall(STATE))["status"] == "picked_up"
    assert await _drain(published) == []


async def test_cancellation_leaves_pending_tells_the_restaurant_and_expires_sooner(
    redis: Redis, published: PubSub
) -> None:
    awaiting = _moved("OrderAwaitingRestaurant", "created", "awaiting_restaurant_acceptance")
    await projection.apply_order_event(redis, awaiting)
    cancelled = _moved(
        "OrderCancelled", "awaiting_restaurant_acceptance", "cancelled", "customer_cancelled"
    )
    assert await projection.apply_order_event(redis, cancelled)

    state = await redis.hgetall(STATE)
    assert (state["status"], state["status_reason"]) == ("cancelled", "customer_cancelled")
    assert await redis.smembers(PENDING) == set()
    assert await redis.ttl(STATE) <= 3600
    types = [(channel, m["type"]) for channel, m in await _drain(published)]
    assert types[-2:] == [(ORDER_CHANNEL, "status"), (RESTAURANT_CHANNEL, "order_cancelled")]


async def test_events_older_than_the_state_lifetime_are_skipped(redis: Redis) -> None:
    old = _placed().model_copy(update={"occurred_at": datetime.now(UTC) - timedelta(days=2)})
    assert not await projection.apply_order_event(redis, old)
    assert await redis.exists(STATE) == 0


async def test_events_that_are_not_about_an_order_are_ignored(redis: Redis) -> None:
    payment = DomainEvent(
        event_id="e-pay",
        event_type="PaymentSuccessful",
        aggregate_id="p-1",
        occurred_at=datetime.now(UTC),
        payload={
            "payment_id": "p-1",
            "order_id": ORDER,
            "customer_id": "u-customer",
            "amount": "12.50",
            "status": "authorized",
        },
    )
    assert not await projection.apply_order_event(redis, payment)
    assert await redis.exists(STATE) == 0


async def test_redis_down_is_an_outage_to_wait_out_not_a_poison_message() -> None:
    unreachable = from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    with pytest.raises(ConnectionError) as exc_info:
        await projection.apply_order_event(unreachable, _placed())
    # So the consumer loop rewinds and retries rather than dead-lettering it.
    assert is_transient(exc_info.value)
    await unreachable.aclose()
