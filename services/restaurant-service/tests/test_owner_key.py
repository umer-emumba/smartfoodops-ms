"""restaurant:{id}:owner for tracking-service (week2-plan D11)."""

from types import SimpleNamespace

import pytest
from app import owners
from app.api import internal
from app.main import app
from httpx import ASGITransport, AsyncClient


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value


async def test_owner_endpoint_returns_and_rewrites_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Redis()
    monkeypatch.setattr(owners, "redis", fake)

    async def get_restaurant(_session: object, restaurant_id: str):  # type: ignore[no-untyped-def]
        return (
            SimpleNamespace(id=restaurant_id, admin_user_id="u-owner")
            if restaurant_id == "r1"
            else None
        )

    monkeypatch.setattr(internal.repository, "get_restaurant", get_restaurant)

    async def _no_session():  # type: ignore[no-untyped-def]
        yield None

    app.dependency_overrides[internal.get_session] = _no_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await client.get("/internal/restaurants/r1/owner")
            missing = await client.get("/internal/restaurants/nope/owner")
    finally:
        app.dependency_overrides.clear()
    assert ok.json() == {"restaurant_id": "r1", "admin_user_id": "u-owner"}
    assert fake.values == {"restaurant:r1:owner": "u-owner"}
    assert missing.status_code == 404
