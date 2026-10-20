"""Test helpers: mint tokens with a throwaway key and make a service's JWKS
store trust it, with no user-service involved. Not for production code."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sfo_auth.keys import JwksKeyStore, TokenSigner
from sfo_auth.roles import Role
from sfo_auth.tokens import create_access_token

# One key per test process.
TEST_SIGNER = TokenSigner(Ed25519PrivateKey.generate())


def trust_test_key(jwks_url: str) -> None:
    JwksKeyStore.shared(jwks_url).pin(TEST_SIGNER.public_keys())


def make_test_token(*, user_id: str, role: Role, jwks_url: str, ttl_minutes: int = 15) -> str:
    """A valid token, and the store behind `jwks_url` (a service's
    `settings.jwt_jwks_url`) is made to accept it."""
    trust_test_key(jwks_url)
    return create_access_token(
        user_id=user_id, role=role, signer=TEST_SIGNER, ttl_minutes=ttl_minutes
    )
