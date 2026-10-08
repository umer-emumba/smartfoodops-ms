"""Shared by the gateway test modules (fixtures live in conftest.py)."""

from app.config import settings
from app.main import app
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token


def auth() -> dict[str, str]:
    token = create_access_token(
        user_id="u1",
        role=Role.CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=15,
    )
    return {"Authorization": f"Bearer {token}"}


def gateway_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://gw"
    )
