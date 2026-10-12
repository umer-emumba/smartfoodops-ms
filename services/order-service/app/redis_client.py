from redis.asyncio import Redis, from_url

from app.config import settings

# Live-tracking state and publishes (app/tracking.py). Short timeouts: they
# are best-effort and must never hold up an order transition.
redis: Redis = from_url(
    settings.redis_url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5
)
