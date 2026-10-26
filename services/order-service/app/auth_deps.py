from sfo_auth import JwksKeyStore, build_auth_dependencies

from app.config import settings

get_current_user, require_roles, get_current_user_optional = build_auth_dependencies(
    key_store=JwksKeyStore.shared(settings.jwt_jwks_url)
)
