import time

import jwt
import pytest
from app import security
from app.config import settings
from app.security import authenticate, is_public_route
from sfo_auth import JwksKeyStore, Role
from sfo_auth.keys import ALGORITHM, ISSUER
from sfo_auth.testing import TEST_SIGNER, make_test_token
from sfo_common.errors import ServiceUnavailableError, UnauthorizedError
from starlette.requests import Request


def _request_with_auth_header(value: str | None) -> Request:
    headers = [(b"authorization", value.encode())] if value else []
    return Request(scope={"type": "http", "headers": headers})


def test_public_routes_classified_correctly() -> None:
    assert is_public_route("POST", "auth", "login")
    assert is_public_route("POST", "auth", "register")
    assert is_public_route("POST", "auth", "refresh")
    assert is_public_route("POST", "auth", "bootstrap-admin")
    assert is_public_route("GET", "restaurants", "")
    assert is_public_route("GET", "restaurants", "0198abc1-2345-7def-8901-abcdef123456")
    assert is_public_route("GET", "restaurants", "0198abc1-2345-7def-8901-abcdef123456/menu")
    assert is_public_route(
        "GET", "restaurants", "0198abc1-2345-7def-8901-abcdef123456/menu/categories"
    )


def test_private_routes_not_misclassified_as_public() -> None:
    # "mine"/"admin" don't match the UUID shape, so they stay private even
    # though they're single path segments just like a real restaurant ID.
    assert not is_public_route("GET", "restaurants", "mine")
    assert not is_public_route("GET", "restaurants", "admin")
    assert not is_public_route("POST", "restaurants", "")
    assert not is_public_route("PATCH", "restaurants", "0198abc1-2345-7def-8901-abcdef123456")
    assert not is_public_route("GET", "orders", "")
    assert not is_public_route("GET", "payments", "by-order/abc")
    assert not is_public_route("GET", "riders", "admin")
    assert not is_public_route("GET", "users", "me")
    # Writes to the same path stay private.
    assert not is_public_route(
        "POST", "restaurants", "0198abc1-2345-7def-8901-abcdef123456/menu/categories"
    )
    assert not is_public_route("GET", "users", "")
    assert not is_public_route("GET", "orders", "admin")


@pytest.fixture
def not_blocked(monkeypatch):
    async def exists(_: str) -> int:
        return 0

    monkeypatch.setattr(security.redis, "exists", exists)


def _token(role: str = "customer") -> str:
    return make_test_token(
        user_id="u1", role=Role(role), jwks_url=settings.jwt_jwks_url, ttl_minutes=15
    )


async def test_private_route_rejects_missing_token(not_blocked) -> None:
    with pytest.raises(UnauthorizedError):
        await authenticate(_request_with_auth_header(None), public=False)


async def test_private_route_rejects_garbage_token(not_blocked) -> None:
    with pytest.raises(UnauthorizedError):
        await authenticate(_request_with_auth_header("Bearer not-a-jwt"), public=False)


async def test_private_route_accepts_valid_token(not_blocked) -> None:
    await authenticate(_request_with_auth_header(f"Bearer {_token()}"), public=False)


async def test_public_route_tolerates_missing_or_bad_token(not_blocked) -> None:
    await authenticate(_request_with_auth_header(None), public=True)
    await authenticate(_request_with_auth_header("Bearer not-a-jwt"), public=True)


def _signed(claims: dict, *, key=None, algorithm: str = ALGORITHM, headers=None) -> str:
    now = int(time.time())
    base = {"iss": ISSUER, "sub": "u1", "role": "customer", "iat": now, "exp": now + 600}
    return jwt.encode(
        {**base, **claims},
        key if key is not None else TEST_SIGNER.private_key,
        algorithm=algorithm,
        headers={"kid": TEST_SIGNER.kid} if headers is None else headers,
    )


async def _rejected(token: str) -> None:
    make_test_token(user_id="u1", role=Role.CUSTOMER, jwks_url=settings.jwt_jwks_url)
    with pytest.raises(UnauthorizedError):
        await authenticate(_request_with_auth_header(f"Bearer {token}"), public=False)


async def test_correctly_signed_token_with_unknown_role_is_invalid(not_blocked) -> None:
    await _rejected(_signed({"role": "superuser"}))


async def test_token_signed_with_another_key_is_rejected(not_blocked) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # Same kid as the trusted key, but not signed by it.
    await _rejected(_signed({}, key=Ed25519PrivateKey.generate()))


async def test_token_with_unknown_key_id_is_rejected(not_blocked) -> None:
    await _rejected(_signed({}, headers={"kid": "not-a-known-key"}))


async def test_token_without_key_id_is_rejected(not_blocked) -> None:
    await _rejected(_signed({}, headers={}))


async def test_hs256_token_using_the_public_key_as_secret_is_rejected(not_blocked) -> None:
    """The classic algorithm-confusion attack: the public key is, well, public."""
    from sfo_auth.keys import _raw_public_bytes

    forged = _signed(
        {},
        key=_raw_public_bytes(TEST_SIGNER.public_key),
        algorithm="HS256",
        headers={"kid": TEST_SIGNER.kid},
    )
    await _rejected(forged)


async def test_unsigned_token_is_rejected(not_blocked) -> None:
    await _rejected(_signed({}, key="", algorithm="none"))


async def test_token_from_another_issuer_is_rejected(not_blocked) -> None:
    await _rejected(_signed({"iss": "someone-else"}))


async def test_expired_token_is_rejected(not_blocked) -> None:
    await _rejected(_signed({"exp": int(time.time()) - 10}))


async def test_503_not_401_when_public_keys_cannot_be_fetched(monkeypatch, not_blocked) -> None:
    async def unreachable() -> dict:
        raise ConnectionError("user-service is down")

    cold = JwksKeyStore("http://user-service.invalid/jwks", fetch=unreachable)
    monkeypatch.setattr(security, "_key_store", cold)
    with pytest.raises(ServiceUnavailableError):
        await authenticate(_request_with_auth_header(f"Bearer {_signed({})}"), public=False)
    # A public route doesn't need the token, so it still works.
    await authenticate(_request_with_auth_header(f"Bearer {_signed({})}"), public=True)
