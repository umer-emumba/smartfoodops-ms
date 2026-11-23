"""The search API's own rules — validation and who may search what. The
retrieval underneath is replaced; tests/test_retrieval_db.py covers it."""

import pytest
from app import retrieval
from app.config import settings
from app.main import app
from app.retrieval import Filters, Hit
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role
from sfo_auth.testing import make_test_token


def _as(user_id: str, role: Role) -> dict[str, str]:
    token = make_test_token(user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Filters]]:
    seen: list[tuple[str, Filters]] = []

    async def retrieve(query: str, filters: Filters) -> list[Hit]:
        seen.append((query, filters))
        return [Hit(kind="item", id="m-1", score=0.5, text="Chicken Biryani", data={"price": 690})]

    monkeypatch.setattr(retrieval, "retrieve", retrieve)
    return seen


async def _get(path: str, headers: dict | None = None, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, params=params, headers=headers)


async def test_search_passes_the_filters(calls) -> None:
    resp = await _get(
        "/v1/ai/search",
        q="spicy",
        lat=33.72,
        lng=73.05,
        max_price=800,
        dietary=["halal", "vegetarian"],
    )
    assert resp.status_code == 200
    assert resp.json()["hits"][0]["id"] == "m-1"
    [(query, f)] = calls
    assert (query, f.kind, f.radius_km, f.max_price, f.dietary) == (
        "spicy",
        "item",
        5.0,
        800,
        ("halal", "vegetarian"),
    )


async def test_radius_only_with_a_location(calls) -> None:
    await _get("/v1/ai/search", q="tea", radius_km=3)
    assert calls[0][1].radius_km is None


@pytest.mark.parametrize(
    "params",
    [
        {"q": "tea", "lat": 33.7},  # lng missing
        {"q": "tea", "kind": "review", "max_price": 500},
        {"q": "tea", "kind": "restaurant", "dietary": ["vegan"]},
        {"q": "tea", "dietary": ["keto"]},
        {"q": "", "kind": "item"},
        {"q": "tea", "kind": "drink"},
    ],
)
async def test_bad_requests(calls, params) -> None:
    assert (await _get("/v1/ai/search", **params)).status_code == 422
    assert calls == []


async def test_history_is_the_callers_own(calls) -> None:
    assert (await _get("/v1/ai/search/history", q="biryani")).status_code == 401
    owner = _as("u-owner", Role.RESTAURANT_ADMIN)
    assert (await _get("/v1/ai/search/history", owner, q="biryani")).status_code == 403
    resp = await _get("/v1/ai/search/history", _as("u-1", Role.CUSTOMER), q="biryani")
    assert resp.status_code == 200
    assert calls[-1][1].customer_id == "u-1"
