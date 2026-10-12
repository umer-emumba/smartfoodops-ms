"""One Redis Pub/Sub connection per replica, shared by all of its SSE
clients (docs/week2-plan.md D8). A channel is subscribed while at least one
local client watches it and unsubscribed when the last one leaves, so each
replica only receives messages for the connections it holds — and holds one
Redis connection, not one per client."""

import asyncio
import contextlib
import logging

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

_logger = logging.getLogger(__name__)


class Subscription:
    """One client's view of a channel: a bounded queue of raw messages."""

    def __init__(self, channel: str, maxsize: int) -> None:
        self.channel = channel
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize)
        # Set when the client fell too far behind and a message was dropped:
        # its stream ends, and the client reconnects for a fresh snapshot.
        self.overflowed = False


class Hub:
    def __init__(self, redis: Redis, *, queue_size: int) -> None:
        self._redis = redis
        self._queue_size = queue_size
        self._pubsub: PubSub | None = None
        self._subscriptions: dict[str, set[Subscription]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def subscribe(self, channel: str) -> Subscription:
        subscription = Subscription(channel, self._queue_size)
        async with self._lock:
            if self._pubsub is None:
                self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            watchers = self._subscriptions.setdefault(channel, set())
            watchers.add(subscription)
            if len(watchers) == 1:
                await self._pubsub.subscribe(channel)
            if self._reader is None or self._reader.done():
                self._reader = asyncio.create_task(self._read())
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            watchers = self._subscriptions.get(subscription.channel)
            if watchers is None:
                return
            watchers.discard(subscription)
            if not watchers:
                del self._subscriptions[subscription.channel]
                if self._pubsub is not None:
                    with contextlib.suppress(RedisError):
                        await self._pubsub.unsubscribe(subscription.channel)

    def watched_channels(self) -> set[str]:
        return set(self._subscriptions)

    async def _read(self) -> None:
        assert self._pubsub is not None
        while True:
            try:
                message = await self._pubsub.get_message(timeout=1.0)
            except RedisError:
                # redis-py reconnects and re-subscribes on the next read.
                _logger.warning("Tracking pub/sub read failed, retrying", exc_info=True)
                await asyncio.sleep(1)
                continue
            if message is None or message.get("type") != "message":
                continue
            for subscription in list(self._subscriptions.get(message["channel"], ())):
                try:
                    subscription.queue.put_nowait(message["data"])
                except asyncio.QueueFull:
                    subscription.overflowed = True

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._pubsub is not None:
            await self._pubsub.aclose()


# The process-wide hub (closed by app.main's lifespan).
def _make_hub() -> Hub:
    from app.config import settings
    from app.redis_client import redis

    return Hub(redis, queue_size=settings.client_queue_size)


hub = _make_hub()
