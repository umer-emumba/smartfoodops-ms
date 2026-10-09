from redis.asyncio import Redis, from_url

from app.config import settings

# Live-tracking publishes (app/tracking.py). Short timeouts: a publish is
# best-effort and must never hold up an order transition.
redis: Redis = from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
