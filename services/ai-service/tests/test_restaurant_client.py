"""Kitchen load (app/restaurant_client.py) and how it and the rating weigh into a
restaurant suggestion (app/retrieval.py). Load is a ranking hint: any
failure to read it is "unknown", never an error."""

import httpx
import pytest
from app import metrics, restaurant_client
from app.retrieval import Load, load_factor, rating_factor


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://restaurant-service", transport=httpx.MockTransport(handler)
    )


async def test_loads_are_read_from_restaurant_service(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        row = {
            "restaurant_id": "r-1",
            "active_orders": 4,
            "max_orders": 10,
            "is_accepting_orders": True,
        }
        return httpx.Response(200, json={"loads": [row]})

    monkeypatch.setattr(restaurant_client, "client", _client(handler))
    assert await restaurant_client.loads(["r-1", "r-2"]) == {"r-1": Load(4, 10, True)}
    assert seen == [("/internal/restaurants/load", b'{"restaurant_ids":["r-1","r-2"]}')]
    assert await restaurant_client.loads([]) == {}
    assert len(seen) == 1  # nothing to ask, no call


@pytest.mark.parametrize(
    "respond",
    [
        lambda r: httpx.Response(503),
        lambda r: httpx.Response(200, json={"unexpected": True}),
        lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow", request=r)),
    ],
)
async def test_any_failure_means_unknown_load(monkeypatch: pytest.MonkeyPatch, respond) -> None:
    monkeypatch.setattr(restaurant_client, "client", _client(respond))
    before = metrics.KITCHEN_LOAD_LOOKUPS.labels("failed")._value.get()
    assert await restaurant_client.loads(["r-1"]) == {}
    assert metrics.KITCHEN_LOAD_LOOKUPS.labels("failed")._value.get() == before + 1


def test_load_factor() -> None:
    assert load_factor(None) == 1.0
    assert load_factor(Load(0, 10, True)) == 1.0
    assert load_factor(Load(9, 10, True)) == pytest.approx(0.55)
    assert Load(10, 10, True).full and Load(0, 10, False).full and not Load(9, 10, True).full
    assert Load(3, 0, True).utilization == 1.0  # no cap set: treat as full


def test_rating_factor_trusts_many_reviews_more_than_one() -> None:
    assert rating_factor(None, 0) == pytest.approx(0.88)  # the prior, 3.5
    one_perfect = rating_factor(5.0, 1)
    many_good = rating_factor(4.6, 50)
    assert many_good > one_perfect > rating_factor(None, 0) > rating_factor(2.0, 30)
    assert 0.6 <= rating_factor(1.0, 1000) < rating_factor(5.0, 1000) <= 1.0
