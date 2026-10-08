from redis.asyncio import Redis

_KEY_PREFIX = "blocked_user:"


def blocked_key(user_id: str) -> str:
    """The deny-list key for a user — the one definition shared by the writer
    (user-service) and the reader (api-gateway)."""
    return f"{_KEY_PREFIX}{user_id}"


async def block_user(redis: Redis, user_id: str, *, ttl_seconds: int) -> None:
    """Called by user-service when an admin blocks/disables a user. TTL is set
    to the access-token lifetime — see docs/technical-documentation.md, "Auth:
    15-minute access tokens, rotating refresh tokens, Redis deny-list" for why
    that bound is safe (the authoritative check is user_db.is_blocked at
    refresh time, this is just the fast-path for an already-issued token)."""
    await redis.set(blocked_key(user_id), "1", ex=ttl_seconds)


async def unblock_user(redis: Redis, user_id: str) -> None:
    await redis.delete(blocked_key(user_id))


async def is_blocked(redis: Redis, user_id: str) -> bool:
    """Checked by api-gateway on every request, in addition to normal JWT
    verification."""
    return await redis.exists(blocked_key(user_id)) == 1
