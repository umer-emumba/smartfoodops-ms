from redis.asyncio import Redis

_KEY_PREFIX = "blocked_user:"


async def block_user(redis: Redis, user_id: str, *, ttl_seconds: int) -> None:
    """Called by user-service when an admin blocks/disables a user. TTL is set
    to the access-token lifetime — see docs/technical-documentation.md#auth for
    why that bound is safe (the authoritative check is user_db.is_blocked at
    refresh time, this is just the fast-path for an already-issued token)."""
    await redis.set(f"{_KEY_PREFIX}{user_id}", "1", ex=ttl_seconds)


async def unblock_user(redis: Redis, user_id: str) -> None:
    await redis.delete(f"{_KEY_PREFIX}{user_id}")


async def is_blocked(redis: Redis, user_id: str) -> bool:
    """Checked by api-gateway on every request, in addition to normal JWT
    verification."""
    return await redis.exists(f"{_KEY_PREFIX}{user_id}") == 1
