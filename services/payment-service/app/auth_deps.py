from sfo_auth import build_auth_dependencies

from app.config import settings

get_current_user, require_roles, get_current_user_optional = build_auth_dependencies(
    jwt_secret=settings.jwt_secret, jwt_algorithm=settings.jwt_algorithm
)
