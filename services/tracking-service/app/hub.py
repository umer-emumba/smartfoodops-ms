"""One Redis Pub/Sub connection per replica, shared by all of its SSE
clients (docs/week2-plan.md D8). A channel is subscribed while at least one
local client watches it and unsubscribed when the last one leaves, so each
replica only receives messages for the connections it holds — and holds one
Redis connection, not one per client.

Pub/Sub doesn't keep messages: whatever is published while the connection is
down is gone. redis-py reconnects and re-subscribes on its own, without
telling us, so the hub registers for every reconnect (and read error) and
then ends every open stream. Each client reconnects and gets a fresh
snapshot, so none is left showing a status it missed."""

import asyncio
import contextlib
import logging

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sfo_observability import quiet

_logger = logging.getLogger(__name__)


class Subscription:
    """One client's view of a channel: a bounded queue of raw messages."""

    def __init__(self, channel: str, maxsize: int) -> None:
        self.channel = channel
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize)
        # Set when the client may have missed a message — it fell too far
        # behind, or the Pub/Sub connection dropped: its stream ends, and the
        # client reconnects for a fresh snapshot.
        self.overflowed = False

    def end(self) -> None:
        """Ends the stream now rather than at its next message or heartbeat."""
        self.overflowed = True
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait("")


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
            first = self._pubsub is None
            if self._pubsub is None:
                self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            watchers = self._subscriptions.setdefault(channel, set())
            watchers.add(subscription)
            if len(watchers) == 1:
                await self._pubsub.subscribe(channel)
            if first and self._pubsub.connection is not None:
                # Registered after the first connect, so it fires on reconnects.
                self._pubsub.connection.register_connect_callback(self._on_reconnect)
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
                with quiet():  # polled every second
                    message = await self._pubsub.get_message(timeout=1.0)
            except RedisError:
                # redis-py reconnects and re-subscribes on the next read.
                _logger.warning("Tracking pub/sub read failed, retrying", exc_info=True)
                self._end_all("read failed")
                await asyncio.sleep(1)
                continue
            if message is None or message.get("type") != "message":
                continue
            for subscription in list(self._subscriptions.get(message["channel"], ())):
                try:
                    subscription.queue.put_nowait(message["data"])
                except asyncio.QueueFull:
                    subscription.overflowed = True

    async def _on_reconnect(self, _connection: object) -> None:
        self._end_all("reconnected")

    def _end_all(self, why: str) -> None:
        ended = [s for watchers in self._subscriptions.values() for s in watchers]
        for subscription in ended:
            subscription.end()
        if ended:
            _logger.warning("Tracking pub/sub %s; ended %d streams so they resync", why, len(ended))

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
