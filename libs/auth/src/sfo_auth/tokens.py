import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel, ValidationError
from sfo_common import new_id

from sfo_auth.keys import ALGORITHM, ISSUER, KeyStore, TokenSigner
from sfo_auth.roles import Role

# Clocks on different hosts drift a little; without slack a token issued a
# second "in the future" (iat) or just expired would be rejected at random.
CLOCK_SKEW_SECONDS = 10


class TokenPayload(BaseModel):
    sub: str  # user id
    role: Role
    exp: int
    iat: int


def create_access_token(*, user_id: str, role: Role, signer: TokenSigner, ttl_minutes: int) -> str:
    """Signed with user-service's private key (EdDSA); the `kid` header says
    which public key verifies it (sfo_auth.keys)."""
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": user_id,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, signer.private_key, algorithm=ALGORITHM, headers={"kid": signer.kid})


async def decode_access_token(token: str, *, keys: KeyStore) -> TokenPayload:
    """Raises jwt.InvalidTokenError (or a subclass) on a bad token — callers
    turn that into an UnauthorizedError — and KeyStoreUnavailable when the
    public keys can't be fetched (a 503, not the token's fault). Signature +
    expiry only, no DB call — see docs/technical-documentation.md, "Auth:
    15-minute access tokens, rotating refresh tokens, Redis deny-list"."""
    header = jwt.get_unverified_header(token)
    # Checked before any key lookup, and decode below pins it again: the
    # header must never choose the algorithm.
    if header.get("alg") != ALGORITHM:
        raise jwt.InvalidAlgorithmError("Unexpected token signing algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str):
        raise jwt.InvalidTokenError("Token has no key ID")
    key = await keys.get(kid)
    if key is None:
        raise jwt.InvalidTokenError("Token was signed with an unknown key")
    raw = jwt.decode(
        token,
        key,
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        leeway=CLOCK_SKEW_SECONDS,
        options={"require": ["iss", "sub", "role", "exp", "iat"]},
    )
    try:
        return TokenPayload(**raw)
    except ValidationError as exc:
        # Correctly signed but malformed claims (e.g. an unknown role) — still
        # an invalid token to callers, not a 500.
        raise jwt.InvalidTokenError("Malformed token claims") from exc


# --- Refresh tokens: NOT JWTs, tracked server-side ---
# (docs/technical-documentation.md, "Auth: 15-minute access tokens, rotating
# refresh tokens, Redis deny-list")
# A refresh token string is "{lookup_id}.{secret}": lookup_id is stored in
# plaintext for O(1) DB lookup, secret is hashed before storage so a DB leak
# alone doesn't hand out usable refresh tokens.
#
# SHA-256, not argon2: a slow hash protects low-entropy secrets (passwords)
# from offline guessing. The secret here is 256 random bits, which can't be
# guessed at any hash speed — argon2 would add ~50ms of CPU to every refresh
# and logout for no security gain.


def hash_refresh_secret(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode()).hexdigest()


def verify_refresh_secret(raw_secret: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_refresh_secret(raw_secret), hashed)


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
