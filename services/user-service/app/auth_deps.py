from sfo_auth import build_auth_dependencies

from app.signing import OwnKeys

# Every service builds its own copy of these — this is the defense-in-depth
# JWT check each service runs independently, not just api-gateway. Here the
# keys are this service's own (the other services fetch them as a JWKS).
# See docs/architecture.md#defense-in-depth-auth.
get_current_user, require_roles, get_current_user_optional = build_auth_dependencies(
    key_store=OwnKeys()
)
