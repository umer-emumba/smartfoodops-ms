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


# ---------- Deliveries and the rider feed ----------

DELIVERY, RIDER_USER = "d-1", "u-rider"
RIDER_STATE, RIDER_CHANNEL = f"tracking:rider:{RIDER_USER}:state", f"tracking:rider:{RIDER_USER}"


def _delivery_event(
    to_status: str,
    *,
    seconds_ago: float,
    delivery_id: str = DELIVERY,
    rider: str | None = None,
    released: str | None = None,
    reason: str | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_id=f"e-{delivery_id}-{to_status}-{seconds_ago}",
        event_type="DeliveryStatusUpdated",
        aggregate_id=delivery_id,
        occurred_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        payload={
            "delivery_id": delivery_id,
            "order_id": ORDER,
            "customer_id": "u-customer",
            "rider_id": "r-1" if rider else None,
            "rider_user_id": rider,
            "from_status": None,
            "to_status": to_status,
            "reason": reason,
            "released_rider_user_id": released,
        },
    )


@pytest_asyncio.fixture
async def rider_feed(redis: Redis) -> AsyncIterator[PubSub]:
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(ORDER_CHANNEL, RIDER_CHANNEL)
    yield pubsub
    await pubsub.aclose()


async def test_offer_reaches_the_rider_and_the_customer(redis: Redis, rider_feed: PubSub) -> None:
    assert await projection.apply_event(
        redis, _delivery_event("pending_assignment", seconds_ago=30)
    )
    assert await projection.apply_event(
        redis, _delivery_event("offered", seconds_ago=20, rider=RIDER_USER)
    )
    rider_state = await redis.hgetall(RIDER_STATE)
    assert (rider_state["delivery_id"], rider_state["order_id"], rider_state["status"]) == (
        DELIVERY,
        ORDER,
        "offered",
    )
    assert (await redis.hgetall(STATE))["delivery_status"] == "offered"
    messages = [(c, m["type"], m.get("status")) for c, m in await _drain(rider_feed)]
    assert messages == [
        (ORDER_CHANNEL, "delivery", "pending_assignment"),
        (ORDER_CHANNEL, "delivery", "offered"),
        (RIDER_CHANNEL, "offer", None),
    ]


async def test_declined_offer_is_released_and_cleared(redis: Redis, rider_feed: PubSub) -> None:
    await projection.apply_event(
        redis, _delivery_event("offered", seconds_ago=20, rider=RIDER_USER)
    )
    declined = _delivery_event(
        "pending_assignment", seconds_ago=10, released=RIDER_USER, reason="rider_declined"
    )
    assert await projection.apply_event(redis, declined)
    assert await redis.exists(RIDER_STATE) == 0
    rider_messages = [m for c, m in await _drain(rider_feed) if c == RIDER_CHANNEL]
    assert [(m["type"], m.get("reason")) for m in rider_messages] == [
        ("offer", None),
        ("released", "rider_declined"),
    ]


async def test_late_release_of_an_old_offer_keeps_the_new_one(redis: Redis) -> None:
    """Two deliveries' events sit on different partitions and can arrive in
    either order: the old offer's release must not wipe the newer offer."""
    await projection.apply_event(
        redis, _delivery_event("offered", seconds_ago=5, delivery_id="d-2", rider=RIDER_USER)
    )
    old_release = _delivery_event(
        "pending_assignment", seconds_ago=30, released=RIDER_USER, reason="rider_expired"
    )
    await projection.apply_event(redis, old_release)
    assert (await redis.hgetall(RIDER_STATE))["delivery_id"] == "d-2"


async def test_redelivered_delivery_event_changes_and_publishes_nothing(
    redis: Redis, rider_feed: PubSub
) -> None:
    offered = _delivery_event("offered", seconds_ago=20, rider=RIDER_USER)
    assert await projection.apply_event(redis, offered)
    await _drain(rider_feed)
    assert not await projection.apply_event(redis, offered)
    assert await _drain(rider_feed) == []


async def test_eta_update_reaches_the_customer_only(redis: Redis, rider_feed: PubSub) -> None:
    pickup = datetime.now(UTC) + timedelta(minutes=8)
    eta = DomainEvent(
        event_id="e-eta",
        event_type="DeliveryEtaUpdated",
        aggregate_id=DELIVERY,
        occurred_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={
            "delivery_id": DELIVERY,
            "order_id": ORDER,
            "customer_id": "u-customer",
            "rider_id": "r-1",
            "status": "assigned",
            "pickup_eta": pickup.isoformat(),
            "dropoff_eta": (pickup + timedelta(minutes=15)).isoformat(),
        },
    )
    assert await projection.apply_event(redis, eta)
    assert (await redis.hgetall(STATE))["pickup_eta"] == pickup.isoformat()
    messages = await _drain(rider_feed)
    assert [(c, m["type"], m["pickup_eta"]) for c, m in messages] == [
        (ORDER_CHANNEL, "delivery", pickup.isoformat())
    ]


async def test_food_ready_reaches_the_customer_and_the_rider(
    redis: Redis, rider_feed: PubSub
) -> None:
    await projection.apply_event(
        redis, _delivery_event("assigned", seconds_ago=20, rider=RIDER_USER)
    )
    await _drain(rider_feed)
    ready_at = datetime.now(UTC) - timedelta(seconds=1)
    ready = DomainEvent(
        event_id="e-ready",
        event_type="DeliveryFoodReady",
        aggregate_id=DELIVERY,
        occurred_at=ready_at,
        payload={
            "delivery_id": DELIVERY,
            "order_id": ORDER,
            "customer_id": "u-customer",
            "rider_id": "r-1",
            "rider_user_id": RIDER_USER,
            "status": "assigned",
            "food_ready_at": ready_at.isoformat(),
        },
    )
    assert await projection.apply_event(redis, ready)
    assert (await redis.hgetall(STATE))["food_ready_at"] == ready_at.isoformat()
    messages = await _drain(rider_feed)
    assert [(c, m["type"]) for c, m in messages] == [
        (ORDER_CHANNEL, "delivery"),
        (RIDER_CHANNEL, "food_ready"),
    ]
    assert messages[0][1]["food_ready_at"] == ready_at.isoformat()
    # A later delivery event keeps it; the rider still holds the delivery.
    await projection.apply_event(
        redis, _delivery_event("picked_up", seconds_ago=0, rider=RIDER_USER)
    )
    state = await redis.hgetall(STATE)
    assert (state["food_ready_at"], state["delivery_status"]) == (ready_at.isoformat(), "picked_up")
    assert (await redis.hgetall(RIDER_STATE))["status"] == "picked_up"


async def test_delivered_clears_the_riders_state(redis: Redis) -> None:
    await projection.apply_event(
        redis, _delivery_event("picked_up", seconds_ago=20, rider=RIDER_USER)
    )
    assert (await redis.hgetall(RIDER_STATE))["status"] == "picked_up"
    await projection.apply_event(
        redis, _delivery_event("delivered", seconds_ago=10, rider=RIDER_USER)
    )
    assert await redis.exists(RIDER_STATE) == 0
