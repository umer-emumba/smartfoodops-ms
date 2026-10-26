"""Customer order tracking and the restaurant new-order feed (week2-plan
step 7) over real SSE, against Redis."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sfo_auth import Role

from tests.conftest import Env, auth

pytestmark = pytest.mark.db

CUSTOMER, OWNER = "u-customer", "u-owner"


async def events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """(event name, parsed data) per SSE event; ("heartbeat", None) per comment."""
    name, data = "message", None
    async for line in response.aiter_lines():
        if line.startswith(":"):
            yield "heartbeat", None
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "" and data is not None:
            yield name, data
            name, data = "message", None


async def _order_state(env: Env, order_id: str = "o-1", status: str = "confirmed") -> None:
    await env.redis.hset(
        f"tracking:order:{order_id}:state",
        mapping={"order_id": order_id, "customer_id": CUSTOMER, "status": status},
    )


async def _publish(env: Env, channel: str, message: dict[str, Any]) -> None:
    # Published only once the stream's subscription is live.
    for _ in range(100):
        if (await env.redis.pubsub_numsub(channel))[0][1] > 0:
            break
        await asyncio.sleep(0.02)
    await env.redis.publish(channel, json.dumps(message))


async def _numsub(env: Env, channel: str) -> int:
    return (await env.redis.pubsub_numsub(channel))[0][1]


# ---------- Customer order tracking ----------


async def test_snapshot_then_live_updates_until_delivered(env: Env) -> None:
    await _order_state(env)
    await env.redis.hset("tracking:order:o-1:state", "rider_lat", "24.81")
    channel = "tracking:order:o-1"
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        async with client.stream("GET", "/v1/tracking/orders/o-1", headers=auth(CUSTOMER)) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            stream = events(resp)
            name, snapshot = await anext(stream)
            assert (name, snapshot["status"], snapshot["rider_lat"]) == (
                "snapshot",
                "confirmed",
                "24.81",
            )
            await _publish(env, channel, {"type": "location", "lat": 24.82, "lng": 67.03})
            await _publish(env, channel, {"type": "status", "status": "delivered"})
            rest = [(n, d) async for n, d in stream if n != "heartbeat"]
    # The stream ended by itself after `delivered`.
    assert rest == [
        ("location", {"type": "location", "lat": 24.82, "lng": 67.03}),
        ("status", {"type": "status", "status": "delivered"}),
    ]


async def test_finished_order_gets_its_snapshot_and_the_stream_ends(env: Env) -> None:
    await _order_state(env, status="cancelled")
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        resp = await client.get("/v1/tracking/orders/o-1", headers=auth(CUSTOMER))
    assert resp.text.startswith("event: snapshot")
    assert resp.text.count("event:") == 1


async def test_heartbeat_while_idle(env: Env) -> None:
    await _order_state(env)
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        async with client.stream("GET", "/v1/tracking/orders/o-1", headers=auth(CUSTOMER)) as resp:
            stream = events(resp)
            await anext(stream)  # snapshot
            assert (await asyncio.wait_for(anext(stream), timeout=2))[0] == "heartbeat"


@pytest.mark.parametrize(
    "who,expected",
    [
        ((CUSTOMER, Role.CUSTOMER), 200),
        (("u-admin", Role.SYSTEM_ADMIN), 200),
        (("someone-else", Role.CUSTOMER), 403),
        ((OWNER, Role.RESTAURANT_ADMIN), 403),
    ],
)
async def test_only_the_customer_or_an_admin(
    env: Env, who: tuple[str, Role], expected: int
) -> None:
    await _order_state(env, status="delivered")  # finished: the stream closes at once
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        resp = await client.get("/v1/tracking/orders/o-1", headers=auth(*who))
    assert resp.status_code == expected
    if expected == 403:
        # Refused before streaming: a normal JSON error, nothing subscribed.
        assert resp.json()["error"]["code"] == "forbidden"
        assert env.hub.watched_channels() == set()


async def test_missing_state_is_rebuilt_from_order_service(env: Env) -> None:
    env.fallback["orders/o-1/tracking"] = {
        "order_id": "o-1",
        "customer_id": CUSTOMER,
        "status": "delivered",
    }
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        resp = await client.get("/v1/tracking/orders/o-1", headers=auth(CUSTOMER))
        missing = await client.get("/v1/tracking/orders/nope", headers=auth(CUSTOMER))
        no_token = await client.get("/v1/tracking/orders/o-1")
    assert resp.status_code == 200 and '"status":"delivered"' in resp.text
    assert "orders/o-1/tracking" in env.fallback_calls
    assert missing.status_code == 404
    assert no_token.status_code == 401


async def test_disconnect_unsubscribes(env: Env) -> None:
    await _order_state(env)
    channel = "tracking:order:o-1"
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        async with client.stream("GET", "/v1/tracking/orders/o-1", headers=auth(CUSTOMER)) as resp:
            await anext(events(resp))
            assert await _numsub(env, channel) == 1
    # The client hung up mid-stream: no subscription is left behind.
    for _ in range(100):
        if await _numsub(env, channel) == 0 and not env.hub.watched_channels():
            break
        await asyncio.sleep(0.05)
    assert await _numsub(env, channel) == 0
    assert env.hub.watched_channels() == set()


async def test_one_redis_subscription_shared_by_many_clients(env: Env) -> None:
    await _order_state(env)
    channel = "tracking:order:o-1"
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        async with (
            client.stream("GET", "/v1/tracking/orders/o-1", headers=auth(CUSTOMER)) as a,
            client.stream("GET", "/v1/tracking/orders/o-1", headers=auth(CUSTOMER)) as b,
        ):
            stream_a, stream_b = events(a), events(b)
            await anext(stream_a)
            await anext(stream_b)
            assert await _numsub(env, channel) == 1  # one Redis subscriber, two clients
            await _publish(env, channel, {"type": "status", "status": "picked_up"})
            assert (await anext(stream_a))[1]["status"] == "picked_up"
            assert (await anext(stream_b))[1]["status"] == "picked_up"


# ---------- Restaurant new-order feed ----------


async def test_restaurant_feed_snapshot_then_new_orders(env: Env) -> None:
    await env.redis.set("restaurant:r-1:owner", OWNER)
    await env.redis.sadd("tracking:restaurant:r-1:pending", "o-1")
    await _order_state(env, "o-1", status="awaiting_restaurant_acceptance")
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        async with client.stream(
            "GET", "/v1/tracking/restaurants/r-1", headers=auth(OWNER, Role.RESTAURANT_ADMIN)
        ) as resp:
            stream = events(resp)
            name, snapshot = await anext(stream)
            assert name == "snapshot"
            assert [o["order_id"] for o in snapshot["pending"]] == ["o-1"]

            loop = asyncio.get_running_loop()
            sent_at = loop.time()
            await _publish(env, "tracking:restaurant:r-1", {"type": "new_order", "order_id": "o-2"})
            name, message = await asyncio.wait_for(anext(stream), timeout=2)
            while name == "heartbeat":
                name, message = await anext(stream)
            assert (name, message["order_id"]) == ("new_order", "o-2")
            assert loop.time() - sent_at < 1  # within a second (plan step 7)


async def test_restaurant_feed_is_owner_only(env: Env) -> None:
    await env.redis.set("restaurant:r-1:owner", OWNER)
    async with httpx.AsyncClient(base_url=env.base_url, timeout=5) as client:
        other = await client.get(
            "/v1/tracking/restaurants/r-1", headers=auth("u-other", Role.RESTAURANT_ADMIN)
        )
        unknown = await client.get(
            "/v1/tracking/restaurants/r-9", headers=auth(OWNER, Role.RESTAURANT_ADMIN)
        )
    assert other.status_code == 403
    assert unknown.status_code == 404
    assert "restaurants/r-9/owner" in env.fallback_calls


# ---------- Slow clients ----------


async def test_client_too_far_behind_is_flagged(env: Env) -> None:
    from app.hub import Hub

    hub = Hub(env.redis, queue_size=2)
    subscription = await hub.subscribe("tracking:order:o-9")
    for n in range(5):
        await _publish(env, "tracking:order:o-9", {"type": "status", "n": n})
    for _ in range(100):
        if subscription.overflowed:
            break
        await asyncio.sleep(0.02)
    assert subscription.overflowed
    await hub.unsubscribe(subscription)
    await hub.close()
