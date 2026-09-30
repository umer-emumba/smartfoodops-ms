from sfo_auth import build_auth_dependencies

from app.config import settings

# Every service builds its own copy of these from its own settings — this is
# the defense-in-depth JWT check each service runs independently, not just
# api-gateway. See docs/architecture.md#defense-in-depth-auth.
get_current_user, require_roles, get_current_user_optional = build_auth_dependencies(
    jwt_secret=settings.jwt_secret, jwt_algorithm=settings.jwt_algorithm
)
