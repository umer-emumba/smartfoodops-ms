import secrets
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel
from sfo_common import new_id

from sfo_auth.roles import Role


class TokenPayload(BaseModel):
    sub: str  # user id
    role: Role
    exp: int
    iat: int


def create_access_token(
    *, user_id: str, role: Role, secret: str, algorithm: str, ttl_minutes: int
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str, *, secret: str, algorithm: str) -> TokenPayload:
    """Raises jwt.InvalidTokenError (or a subclass) on bad signature/expiry —
    callers turn that into an UnauthorizedError. Signature + expiry only, no DB
    call — see docs/technical-documentation.md#auth."""
    raw = jwt.decode(token, secret, algorithms=[algorithm])
    return TokenPayload(**raw)


# --- Refresh tokens: NOT JWTs, tracked server-side (see docs/technical-documentation.md#auth) ---
# A refresh token string is "{lookup_id}.{secret}": lookup_id is stored in
# plaintext for O(1) DB lookup, secret is hashed (sfo_auth.passwords) before
# storage so a DB leak alone doesn't hand out usable refresh tokens.


def generate_refresh_token() -> tuple[str, str, str]:
    """Returns (lookup_id, raw_secret, full_token_string)."""
    lookup_id = new_id()
    raw_secret = secrets.token_urlsafe(32)
    return lookup_id, raw_secret, f"{lookup_id}.{raw_secret}"


def parse_refresh_token(token: str) -> tuple[str, str]:
    """Returns (lookup_id, raw_secret). Raises ValueError if malformed."""
    lookup_id, _, raw_secret = token.partition(".")
    if not lookup_id or not raw_secret:
        raise ValueError("malformed refresh token")
    return lookup_id, raw_secret
