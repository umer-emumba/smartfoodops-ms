from fastapi import Request
from sfo_common.errors import TooManyRequestsError

from app.redis_client import redis

# Atomic increment-with-expiry in one Redis round-trip — the PEXPIRE only
# fires on the first hit in a window, so a key's TTL is never reset by
# subsequent requests within the same window (a plain INCR + separate EXPIRE
# would race and effectively extend the window forever under sustained load).
_LUA_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class RateLimiter:
    """Fixed-window Redis rate limiter, keyed by client IP + a caller-supplied
    bucket name.

    Replaces fastapi-limiter, which broke twice in this project: once across
    its own 0.1.x/0.2.x API split (see docs/PROGRESS.md), and again against
    this FastAPI version's newer lazy-router internals — its RateLimiter
    dependency inspects `request.app.routes` expecting every entry to be a
    plain APIRoute with a `.path` attribute, which no longer holds now that
    FastAPI wraps included routers in a different object. This implementation
    doesn't touch FastAPI route internals at all, so it isn't exposed to that
    class of breakage.
    """

    def __init__(self, *, times: int, seconds: int, bucket: str):
        self.times = times
        self.window_ms = seconds * 1000
        self.bucket = bucket

    async def __call__(self, request: Request) -> None:
        client_id = request.client.host if request.client else "unknown"
        key = f"rate_limit:{self.bucket}:{client_id}"
        current = await redis.eval(_LUA_INCR_WITH_TTL, 1, key, str(self.window_ms))
        if int(current) > self.times:
            ttl_ms = await redis.pttl(key)
            raise TooManyRequestsError(
                "Too many requests", details={"retry_after_ms": max(ttl_ms, 0)}
            )
