import logging

from fastapi import Request
from redis.exceptions import RedisError
from sfo_common.errors import TooManyRequestsError

from app.redis_client import redis
from app.security import verified_token

_logger = logging.getLogger(__name__)

# Atomic increment-with-expiry in one Redis round-trip — the PEXPIRE only
# fires on the first hit in a window, so a key's TTL is never reset by
# subsequent requests within the same window (a plain INCR + separate EXPIRE
# would race and effectively extend the window forever under sustained load).
# Returns the remaining TTL too, so a rejection needs no second round-trip.
_LUA_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('PTTL', KEYS[1])}
"""


def _client_key(request: Request) -> str:
    """A valid bearer token → its user ID; otherwise the client IP.

    Not IP for logged-in users: mobile carriers put many customers behind one
    shared IP (CGNAT), so a per-IP limit throttles unrelated people together.
    Not user+IP either: a user who changes IP (Wi-Fi → mobile data, or on
    purpose via proxies) would get a fresh limit every time. Only a verified
    token counts — an unverified `sub` claim could be forged to spend someone
    else's limit.
    """
    payload = verified_token(request)
    if payload is not None:
        return f"user:{payload.sub}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimiter:
    """Fixed-window Redis rate limiter, keyed by user (or IP when anonymous) +
    a caller-supplied bucket name.

    Replaces fastapi-limiter, which broke against this FastAPI version's newer
    router internals (see docs/PROGRESS.md, "Docker verification") — its RateLimiter
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
        key = f"rate_limit:{self.bucket}:{_client_key(request)}"
        try:
            current, ttl_ms = await redis.eval(_LUA_INCR_WITH_TTL, 1, key, str(self.window_ms))
        except RedisError:
            # Fail open: rate limiting is abuse protection, not correctness —
            # a Redis outage shouldn't take every request down with it. (The
            # blocked-user check in app/security.py deliberately fails closed.)
            _logger.warning("Rate limiter unavailable, allowing request", exc_info=True)
            return
        if int(current) > self.times:
            raise TooManyRequestsError(
                "Too many requests", details={"retry_after_ms": max(int(ttl_ms), 0)}
            )
