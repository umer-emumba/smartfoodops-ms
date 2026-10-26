"""`restaurant:{id}:owner` in Redis — the restaurant admin's user ID, which
tracking-service checks before streaming a restaurant's new-order feed
(docs/week2-plan.md D11). Written when the restaurant is created; ownership
never changes after that, so the key has no expiry. If it's missing,
tracking-service calls GET /internal/restaurants/{id}/owner, which writes it
again."""

import logging

from redis.exceptions import RedisError

from app.redis_client import redis

_logger = logging.getLogger(__name__)


def owner_key(restaurant_id: str) -> str:
    return f"restaurant:{restaurant_id}:owner"


async def record(restaurant_id: str, admin_user_id: str) -> None:
    try:
        await redis.set(owner_key(restaurant_id), admin_user_id)
    except RedisError:
        _logger.warning(
            "Restaurant owner key write failed",
            exc_info=True,
            extra={"extra_fields": {"restaurant_id": restaurant_id}},
        )
