import re

import jwt
from fastapi import Request
from redis.exceptions import RedisError
from sfo_auth import JwksKeyStore, KeyStoreUnavailable, decode_access_token
from sfo_auth.denylist import blocked_key
from sfo_auth.tokens import TokenPayload
from sfo_common.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError

from app.config import settings
from app.redis_client import redis

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# (method, resource, rest_of_path pattern) the gateway forwards without
# requiring a bearer token — everything else is rejected here before it ever
# reaches a backend service. Fail-closed by design: a new private route needs
# no gateway change, a new public one does (docs/architecture.md#gateway-side-
# auth-enforcement). Downstream services still independently re-verify
# (docs/architecture.md#defense-in-depth-auth) — this is an added short-
# circuit, not a replacement for that.
_PUBLIC_ROUTES: list[tuple[str, str, re.Pattern[str]]] = [
    # register/login issue tokens; refresh/logout authenticate via a refresh
    # token in the body, not a bearer JWT. (There is no admin-creating
    # endpoint: the first admin comes from user-service's app/create_admin.py.)
    ("POST", "auth", re.compile(r"^(register|login|refresh|logout)$")),
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


# The public keys that verify tokens, fetched from user-service's JWKS and
# cached (sfo_auth.keys). The gateway holds public keys only.
_key_store = JwksKeyStore.shared(settings.jwt_jwks_url)


async def verified_token(request: Request) -> TokenPayload | None:
    """The request's bearer token, signature/expiry/claims verified — or None
    if there's no token or it's invalid. Decoded at most once per request and
    cached on request.state: the rate limiter (which runs first, keying on the
    user ID) and authenticate() both need it. If the public keys can't be
    fetched the result is None too, but `request.state.keys_unavailable` is
    set, so authenticate() answers 503 rather than blaming the token."""
    if hasattr(request.state, "token_payload"):
        return request.state.token_payload
    payload = None
    if _has_bearer(request):
        try:
            payload = await decode_access_token(
                request.headers["authorization"][7:], keys=_key_store
            )
        except jwt.InvalidTokenError:
            payload = None
        except KeyStoreUnavailable:
            request.state.keys_unavailable = True
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
    payload = await verified_token(request)
    if payload is None:
        if public:
            return
        if getattr(request.state, "keys_unavailable", False):
            raise ServiceUnavailableError("Can't verify tokens right now")
        raise UnauthorizedError("Invalid or expired token")
    # Normally already fetched by the rate limiter in the same Redis
    # round-trip (app/rate_limit.py); checked here only if it wasn't.
    blocked = getattr(request.state, "blocked", None)
    if blocked is None:
        try:
            blocked = await redis.exists(blocked_key(payload.sub))
        except RedisError as exc:
            # Fail closed: without the deny-list the gateway can't honour a block.
            raise ServiceUnavailableError("Auth deny-list is unavailable") from exc
    if blocked:
        raise ForbiddenError("Account is blocked")
