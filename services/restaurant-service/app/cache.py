import logging
from typing import Literal

from fastapi import Response
from redis.exceptions import RedisError

from app.redis_client import redis

_logger = logging.getLogger(__name__)

# Public browsing (restaurant detail, menu, categories) is by far the
# highest-traffic read path and identical for every caller, so it's cached.
#
# - Only an *active* restaurant's views are ever stored, so a cache hit is
#   always safe to serve publicly without re-checking visibility.
# - Every write to a restaurant (profile, status, categories, items) calls
#   invalidate() after commit. TTL bounds staleness if an invalidation is
#   lost (Redis blip, or a reader re-filling the cache with pre-commit data).
# - Redis is never required: any error is a cache miss, served from Postgres.
# Prices shown here may be up to a TTL stale; the price actually charged is
# always re-read fresh by order-service (app/api/internal.py), never cached.
View = Literal["detail", "menu", "categories"]
_VIEWS: tuple[View, ...] = ("detail", "menu", "categories")
_TTL_SECONDS = 60

# What browsers/CDNs may do with the response. Shorter than the Redis TTL,
# since a client-side copy can't be invalidated at all.
PUBLIC_CACHE_CONTROL = "public, max-age=30"
PRIVATE_CACHE_CONTROL = "private, no-store"


def _key(restaurant_id: str, view: View) -> str:
    return f"restaurant-cache:v1:{restaurant_id}:{view}"


async def get(restaurant_id: str, view: View) -> bytes | None:
    try:
        value = await redis.get(_key(restaurant_id, view))
    except RedisError:
        _logger.warning("Restaurant cache read failed", exc_info=True)
        return None
    # The client doesn't decode responses, so this is bytes; the redis-py
    # stubs just can't express that.
    return value.encode() if isinstance(value, str) else value


async def put(restaurant_id: str, view: View, body: bytes) -> None:
    try:
        await redis.set(_key(restaurant_id, view), body, ex=_TTL_SECONDS)
    except RedisError:
        _logger.warning("Restaurant cache write failed", exc_info=True)


async def invalidate(restaurant_id: str) -> None:
    """Call after committing any change to this restaurant or its menu."""
    try:
        await redis.delete(*(_key(restaurant_id, v) for v in _VIEWS))
    except RedisError:
        _logger.warning("Restaurant cache invalidation failed", exc_info=True)


def json_response(body: bytes, *, public: bool) -> Response:
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": PUBLIC_CACHE_CONTROL if public else PRIVATE_CACHE_CONTROL},
    )
