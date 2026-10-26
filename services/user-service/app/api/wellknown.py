from fastapi import APIRouter, Response

from app.signing import get_signer

# Not under /v1: api-gateway only forwards /v1/*, so this is reachable by the
# other services inside the network and not through the public gateway.
router = APIRouter(tags=["auth"])


@router.get("/.well-known/jwks.json")
async def jwks(response: Response) -> dict:
    """The public keys that verify access tokens (RFC 7517 JWKS). Every other
    service fetches this and caches it (sfo_auth.keys.JwksKeyStore). Public
    keys only; the signing key never leaves this service."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_signer().jwks()
