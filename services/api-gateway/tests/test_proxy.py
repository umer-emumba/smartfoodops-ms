"""Gateway behaviour when talking to backends — every failure mode must come
back as the standard JSON error envelope, never a plain-text 500."""

import httpx
import pytest
from app import proxy, rate_limit, security
from app.config import settings
from app.main import app
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token


def _auth() -> dict[str, str]:
    token = create_access_token(
        user_id="u1",
        role=Role.CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=15,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def gateway(monkeypatch):
    """Gateway with Redis stubbed out and a swappable fake backend."""

    async def allow(*_a, **_k) -> int:
        return 1

    async def not_blocked(_: str) -> int:
        return 0

    monkeypatch.setattr(rate_limit.redis, "eval", allow)
    monkeypatch.setattr(security.redis, "exists", not_blocked)

    def use_backend(handler) -> None:
        monkeypatch.setattr(
            proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    return use_backend


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://gw"
    )


@pytest.mark.asyncio
async def test_backend_unreachable_is_502(gateway) -> None:
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    gateway(handler)
    async with _client() as c:
        r = await c.get("/v1/orders", headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "bad_gateway"


@pytest.mark.asyncio
async def test_backend_timeout_is_504(gateway) -> None:
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    gateway(handler)
    async with _client() as c:
        r = await c.get("/v1/orders", headers=_auth())
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "gateway_timeout"


@pytest.mark.asyncio
async def test_blocked_user_is_403_envelope(gateway, monkeypatch) -> None:
    async def blocked(_: str) -> int:
        return 1

    gateway(lambda request: httpx.Response(200, json=[]))
    monkeypatch.setattr(security.redis, "exists", blocked)
    async with _client() as c:
        r = await c.get("/v1/orders", headers=_auth())
    assert r.status_code == 403
    assert r.json()["error"]["message"] == "Account is blocked"


@pytest.mark.asyncio
async def test_forwards_and_strips_hop_by_hop_headers(gateway) -> None:
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(
            200, json={"ok": True}, headers={"Keep-Alive": "timeout=5", "X-Backend": "1"}
        )

    gateway(handler)
    async with _client() as c:
        r = await c.get(
            "/v1/orders",
            headers={**_auth(), "Keep-Alive": "timeout=5", "X-Request-ID": "bad id\nwith newline"},
        )
    assert r.status_code == 200
    assert "keep-alive" not in seen
    assert "keep-alive" not in r.headers
    assert r.headers["x-backend"] == "1"
    # A malformed client correlation ID is replaced, not propagated.
    assert "\n" not in seen["x-request-id"]


@pytest.mark.asyncio
async def test_dot_dot_path_rejected(gateway) -> None:
    gateway(lambda request: httpx.Response(200, json={"leaked": True}))
    async with _client() as c:
        r = await c.get("/v1/orders/%2e%2e/%2e%2e/openapi.json", headers=_auth())
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_docs_serves_gateway_index() -> None:
    async with _client() as c:
        r = await c.get("/docs")
    assert r.status_code == 200
    assert "SmartFoodOps — API Documentation" in r.text
