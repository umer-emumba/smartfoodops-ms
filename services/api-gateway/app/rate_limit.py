import logging

from fastapi import Request
from redis.exceptions import RedisError
from sfo_auth.denylist import blocked_key
from sfo_auth.tokens import TokenPayload
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


def _client_key(request: Request, payload: TokenPayload | None) -> str:
    """A valid bearer token → its user ID; otherwise the client IP.

    Not IP for logged-in users: mobile carriers put many customers behind one
    shared IP (CGNAT), so a per-IP limit throttles unrelated people together.
    Not user+IP either: a user who changes IP (Wi-Fi → mobile data, or on
    purpose via proxies) would get a fresh limit every time. Only a verified
    token counts — an unverified `sub` claim could be forged to spend someone
    else's limit.
    """
    if payload is not None:
        return f"user:{payload.sub}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def _redis_checks(
    rate_key: str, window_ms: int, deny_key: str | None
) -> tuple[int, int, bool | None]:
    """The gateway's Redis work for one request in a single round-trip
    (pipelined): the rate-limit counter and its remaining TTL, plus — for a
    request with a valid token — whether that user is on the deny-list.
    Returns (count, ttl_ms, blocked); blocked is None when not checked."""
    async with redis.pipeline(transaction=False) as pipe:
        pipe.eval(_LUA_INCR_WITH_TTL, 1, rate_key, str(window_ms))
        if deny_key is not None:
            pipe.exists(deny_key)
        results = await pipe.execute()
    current, ttl_ms = results[0]
    blocked = bool(results[1]) if deny_key is not None else None
    return int(current), int(ttl_ms), blocked


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
        payload = verified_token(request)
        key = f"rate_limit:{self.bucket}:{_client_key(request, payload)}"
        deny_key = blocked_key(payload.sub) if payload is not None else None
        try:
            current, ttl_ms, blocked = await _redis_checks(key, self.window_ms, deny_key)
        except RedisError:
            # Fail open: rate limiting is abuse protection, not correctness —
            # a Redis outage shouldn't take every request down with it. The
            # deny-list result is left unset, so app/security.authenticate
            # does its own check, which deliberately fails closed.
            _logger.warning("Rate limiter unavailable, allowing request", exc_info=True)
            return
        if blocked is not None:
            # Read by app/security.authenticate — saves it a second round-trip.
            request.state.blocked = blocked
        if current > self.times:
            raise TooManyRequestsError(
                "Too many requests", details={"retry_after_ms": max(ttl_ms, 0)}
            )
