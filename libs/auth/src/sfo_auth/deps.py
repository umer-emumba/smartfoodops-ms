from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sfo_common.errors import ForbiddenError, UnauthorizedError

from sfo_auth.roles import Role
from sfo_auth.tokens import decode_access_token

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    role: Role


def build_auth_dependencies(
    *, jwt_secret: str, jwt_algorithm: str
) -> tuple[Callable, Callable, Callable]:
    """Every service calls this once at startup with its own settings and gets
    back (get_current_user, require_roles, get_current_user_optional) bound to
    that secret.

    This is the defense-in-depth check every service runs independently, not
    just api-gateway — see docs/architecture.md#defense-in-depth-auth. Verifies
    signature + expiry only, no DB/Redis call, so it stays fast on every request.
    """

    async def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> CurrentUser:
        if credentials is None:
            raise UnauthorizedError("Missing bearer token")
        try:
            payload = decode_access_token(
                credentials.credentials, secret=jwt_secret, algorithm=jwt_algorithm
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("Invalid or expired token") from exc
        return CurrentUser(id=payload.sub, role=payload.role)

    def require_roles(*allowed: Role) -> Callable:
        async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
            if user.role not in allowed:
                raise ForbiddenError(f"Role {user.role} is not permitted for this action")
            return user

        return _check

    async def get_current_user_optional(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> CurrentUser | None:
        """Like get_current_user, but returns None instead of raising when no or
        an invalid bearer token is present — for endpoints that are public but
        need to show more to an authenticated owner/admin (e.g. a restaurant
        owner viewing their own not-yet-approved profile)."""
        if credentials is None:
            return None
        try:
            payload = decode_access_token(
                credentials.credentials, secret=jwt_secret, algorithm=jwt_algorithm
            )
        except jwt.InvalidTokenError:
            return None
        return CurrentUser(id=payload.sub, role=payload.role)

    return get_current_user, require_roles, get_current_user_optional
