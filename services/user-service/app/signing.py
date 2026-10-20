"""The access-token signing key (sfo_auth.keys). Loaded on first use, and
once at start-up by app.main's lifespan, so a missing or invalid key file
stops the service from starting instead of failing the first login."""

from functools import cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sfo_auth import TokenSigner

from app.config import settings


@cache
def get_signer() -> TokenSigner:
    return TokenSigner.from_files(
        settings.jwt_private_key_file, settings.jwt_additional_public_keys_file
    )


class OwnKeys:
    """user-service verifies tokens against its own keys; no JWKS round trip."""

    async def get(self, kid: str) -> Ed25519PublicKey | None:
        return get_signer().public_keys().get(kid)
