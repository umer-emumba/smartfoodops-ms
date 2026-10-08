import httpx
import pytest
from app import proxy, rate_limit, security


@pytest.fixture
def gateway(monkeypatch):
    """Gateway with Redis stubbed out and a swappable fake backend."""

    async def allow(_key, _window, deny_key):
        # (count in window, remaining TTL ms, blocked — None if not checked)
        return 1, 60_000, (False if deny_key else None)

    async def not_blocked(_: str) -> int:
        return 0

    monkeypatch.setattr(rate_limit, "_redis_checks", allow)
    monkeypatch.setattr(security.redis, "exists", not_blocked)

    def use_backend(handler) -> None:
        monkeypatch.setattr(
            proxy,
            "clients",
            {
                name: httpx.AsyncClient(transport=httpx.MockTransport(handler))
                for name in proxy.clients
            },
        )

    return use_backend
