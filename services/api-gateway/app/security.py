import re

import jwt
from fastapi import Request
from redis.exceptions import RedisError
from sfo_auth import decode_access_token
from sfo_auth.tokens import TokenPayload
from sfo_common.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError

from app.config import settings
from app.redis_client import redis

_DENYLIST_PREFIX = "blocked_user:"

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# (method, resource, rest_of_path pattern) the gateway forwards without
# requiring a bearer token — everything else is rejected here before it ever
# reaches a backend service. Fail-closed by design: a new private route needs
# no gateway change, a new public one does (docs/architecture.md#gateway-side-
# auth-enforcement). Downstream services still independently re-verify
# (docs/architecture.md#defense-in-depth-auth) — this is an added short-
# circuit, not a replacement for that.
_PUBLIC_ROUTES: list[tuple[str, str, re.Pattern[str]]] = [
    # register/login issue tokens; refresh/logout/bootstrap-admin authenticate
    # via a refresh token or shared secret in the body/header, not a bearer JWT.
    ("POST", "auth", re.compile(r"^(register|login|refresh|logout|bootstrap-admin)$")),
    # Restaurant browsing is public; "mine"/"admin" are private management
    # views, distinguished from a real ID by not matching the UUID shape.
    ("GET", "restaurants", re.compile(r"^$")),
    ("GET", "restaurants", re.compile(rf"^{_UUID}$")),
    ("GET", "restaurants", re.compile(rf"^{_UUID}/menu$")),
    ("GET", "restaurants", re.compile(rf"^{_UUID}/menu/categories$")),
    ("GET", "restaurants", re.compile(rf"^{_UUID}/promotions$")),
]


def is_public_route(method: str, resource: str, rest_of_path: str) -> bool:
    return any(
        method == m and resource == r and pattern.match(rest_of_path)
        for m, r, pattern in _PUBLIC_ROUTES
    )


def _has_bearer(request: Request) -> bool:
    return request.headers.get("authorization", "").lower().startswith("bearer ")


def verified_token(request: Request) -> TokenPayload | None:
    """The request's bearer token, signature/expiry/claims verified — or None
    if there's no token or it's invalid. Decoded at most once per request and
    cached on request.state: the rate limiter (which runs first, keying on the
    user ID) and authenticate() both need it."""
    if hasattr(request.state, "token_payload"):
        return request.state.token_payload
    payload = None
    if _has_bearer(request):
        try:
            payload = decode_access_token(
                request.headers["authorization"][7:],
                secret=settings.jwt_secret,
                algorithm=settings.jwt_algorithm,
            )
        except jwt.InvalidTokenError:
            payload = None
    request.state.token_payload = payload
    return payload


async def authenticate(request: Request, *, public: bool) -> None:
    """The gateway's one auth step: decodes the bearer token once, then
    checks the Redis deny-list for it.

    - Private route: a missing/invalid/expired token is rejected here (401),
      before a backend is ever called.
    - Public route: no token, or a bad one, is fine — the route doesn't need
      one, and the backend decides for itself (get_current_user_optional).
    - Any valid token, on any route: a blocked user gets 403. This is the
      fast path for an already-issued token; see
      docs/technical-documentation.md, "Auth: 15-minute access tokens...".

    Runs inside the route (not as middleware) so its errors go through the
    shared exception handlers and get the normal JSON envelope + CORS headers.
    """
    if not _has_bearer(request):
        if public:
            return
        raise UnauthorizedError("Missing bearer token")
    payload = verified_token(request)
    if payload is None:
        if public:
            return
        raise UnauthorizedError("Invalid or expired token")
    try:
        blocked = await redis.exists(f"{_DENYLIST_PREFIX}{payload.sub}")
    except RedisError as exc:
        # Fail closed: without the deny-list the gateway can't honour a block.
        raise ServiceUnavailableError("Auth deny-list is unavailable") from exc
    if blocked:
        raise ForbiddenError("Account is blocked")
