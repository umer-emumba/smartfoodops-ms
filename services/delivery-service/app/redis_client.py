from redis.asyncio import Redis, from_url

from app.config import settings

# Rider positions (app/rider_locations.py) and live-tracking publishes
# (app/tracking.py). Responses decoded to str. Short timeouts: a Redis blip
# must not stall a location update.
redis: Redis = from_url(
    settings.redis_url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5
)
