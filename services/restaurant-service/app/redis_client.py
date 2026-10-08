from redis.asyncio import Redis, from_url

from app.config import settings

# Bytes in, bytes out — the cache stores serialized JSON response bodies.
redis: Redis = from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
