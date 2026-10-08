import time

import jwt
import pytest
from app import security
from app.config import settings
from app.security import authenticate, is_public_route
from sfo_auth import Role, create_access_token
from sfo_common.errors import UnauthorizedError
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
    return create_access_token(
        user_id="u1",
        role=Role(role),
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=15,
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


async def test_correctly_signed_token_with_unknown_role_is_invalid(not_blocked) -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "role": "superuser", "iat": now, "exp": now + 600},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(UnauthorizedError):
        await authenticate(_request_with_auth_header(f"Bearer {token}"), public=False)
