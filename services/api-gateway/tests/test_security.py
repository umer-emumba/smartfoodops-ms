import pytest
from sfo_auth import Role, create_access_token
from sfo_common.errors import UnauthorizedError
from starlette.requests import Request

from app.config import settings
from app.security import is_public_route, require_valid_bearer_token


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


def test_require_valid_bearer_token_rejects_missing_header() -> None:
    with pytest.raises(UnauthorizedError):
        require_valid_bearer_token(_request_with_auth_header(None))


def test_require_valid_bearer_token_rejects_garbage_token() -> None:
    with pytest.raises(UnauthorizedError):
        require_valid_bearer_token(_request_with_auth_header("Bearer not-a-jwt"))


def test_require_valid_bearer_token_accepts_valid_token() -> None:
    token = create_access_token(
        user_id="u1",
        role=Role.CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=15,
    )
    require_valid_bearer_token(_request_with_auth_header(f"Bearer {token}"))
