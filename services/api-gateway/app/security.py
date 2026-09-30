import re

import jwt
from fastapi import Request
from sfo_auth import decode_access_token
from sfo_common.errors import ForbiddenError, UnauthorizedError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

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
    ("GET", "restaurants", re.compile(rf"^{_UUID}/promotions$")),
]


def is_public_route(method: str, resource: str, rest_of_path: str) -> bool:
    return any(
        method == m and resource == r and pattern.match(rest_of_path)
        for m, r, pattern in _PUBLIC_ROUTES
    )


def require_valid_bearer_token(request: Request) -> None:
    """Gateway-side rejection for private routes — verifies signature + expiry
    before the request is forwarded to a backend, instead of relying entirely
    on the backend to 401 it. Raises UnauthorizedError (401) if missing/bad."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise UnauthorizedError("Missing bearer token")
    token = auth_header[7:]
    try:
        decode_access_token(token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc


class DenyListGuardMiddleware(BaseHTTPMiddleware):
    """The one auth check that's specific to api-gateway rather than every
    service: fast-path blocking via the Redis deny-list (see
    docs/technical-documentation.md#auth). If the bearer token is absent,
    malformed, or fails verification, this middleware does nothing and lets
    the downstream service's own defense-in-depth check decide — this
    middleware's only job is the deny-list, not full authorization.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
            try:
                payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
                user_id = payload.get("sub")
                if user_id and await redis.exists(f"{_DENYLIST_PREFIX}{user_id}"):
                    raise ForbiddenError("Account is blocked")
            except jwt.InvalidTokenError:
                pass  # not this middleware's job — downstream will reject it
        return await call_next(request)
