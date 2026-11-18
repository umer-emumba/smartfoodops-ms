"""Retrieval against a real Qdrant (fake embeddings, so ranking here is
BM25's and the point is the filters): what a search may never return, and
that each filter narrows the results as asked."""

from decimal import Decimal

import pytest
from app.indexer import Indexer
from app.retrieval import Filters, Load, Retriever
from app.store import Store
from sfo_events import EventType

from tests.conftest import FakeEmbedder
from tests.test_indexer_db import _event, delivered, item, restaurant

pytestmark = pytest.mark.db

F7 = (33.72, 73.05)  # the test restaurant
KARACHI = (24.86, 67.01)


async def _catalog(store: Store) -> None:
    indexer = Indexer(store)
    await indexer.apply(restaurant(1))
    await indexer.apply(item(2))  # Chicken Biryani, 690, halal, spicy
    await indexer.apply(
        item(
            3,
            menu_item_id="m-2",
            name="Veggie Biryani",
            price=Decimal("450"),
            dietary_tags=["vegetarian", "halal"],
        )
    )
    await indexer.apply(item(4, menu_item_id="m-3", name="Mutton Biryani", is_available=False))
    await indexer.apply(item(5, menu_item_id="m-4", name="Egg Biryani", in_stock=False))
    await indexer.apply(item(6, menu_item_id="m-5", name="Beef Biryani"))
    deleted = {"menu_item_id": "m-5", "restaurant_id": "r-1", "source_version": 7}
    await indexer.apply(_event(EventType.MENU_ITEM_DELETED, deleted))
    # A second restaurant, suspended: none of its items may show.
    await indexer.apply(restaurant(1, restaurant_id="r-2", name="Closed Kitchen"))
    await indexer.apply(
        item(
            2,
            menu_item_id="m-9",
            restaurant_id="r-2",
            restaurant_name="Closed Kitchen",
            name="Prawn Biryani",
        )
    )
    suspended = {
        "restaurant_id": "r-2",
        "from_status": "active",
        "to_status": "suspended",
        "source_version": 3,
    }
    await indexer.apply(_event(EventType.RESTAURANT_STATUS_CHANGED, suspended))


async def _ids(retriever: Retriever, q: str, **filters) -> set[str]:
    return {h.id for h in await retriever.retrieve(q, Filters(**filters))}


async def test_only_orderable_items_from_active_restaurants(
    store: Store, embedder: FakeEmbedder
) -> None:
    await _catalog(store)
    retriever = Retriever(store._client, embedder)
    assert await _ids(retriever, "biryani") == {"m-1", "m-2"}


async def test_filters_narrow_the_results(store: Store, embedder: FakeEmbedder) -> None:
    await _catalog(store)
    r = Retriever(store._client, embedder)
    assert await _ids(r, "biryani", max_price=500) == {"m-2"}
    assert await _ids(r, "biryani", dietary=("vegetarian",)) == {"m-2"}
    assert await _ids(r, "biryani", dietary=("vegetarian", "vegan")) == set()
    assert await _ids(r, "biryani", lat=F7[0], lng=F7[1], radius_km=2) == {"m-1", "m-2"}
    assert await _ids(r, "biryani", lat=KARACHI[0], lng=KARACHI[1], radius_km=50) == set()
    assert await _ids(r, "biryani", restaurant_id="r-9") == set()
    assert await _ids(r, "biryani", kind="restaurant") == {"r-1"}


async def test_cuisines_group_their_dishes(store: Store, embedder: FakeEmbedder) -> None:
    await _catalog(store)
    hits = await Retriever(store._client, embedder).retrieve("biryani", Filters(kind="cuisine"))
    assert [h.id for h in hits] == ["Pakistani"]
    assert {i["id"] for i in hits[0].data["items"]} == {"m-1", "m-2"}


async def test_history_is_only_the_callers(store: Store, embedder: FakeEmbedder) -> None:
    indexer = Indexer(store)
    await indexer.apply(restaurant(1))
    await indexer.apply(delivered("o-1"))  # customer u-1
    r = Retriever(store._client, embedder)
    hits = await r.retrieve("biryani", Filters(customer_id="u-1"))
    assert [(h.kind, h.id) for h in hits] == [("order", "o-1")]
    assert "text" not in hits[0].data and hits[0].text.startswith("Order from")
    assert await r.retrieve("biryani", Filters(customer_id="u-2")) == []


# --- Week 5 step 1: the assistant's filters and restaurant suggestions ---


async def _two_kitchens(store: Store) -> None:
    """r-1 Pakistani (rated 4.6 by 20), r-3 Thai (rated 3.0 by 2)."""
    indexer = Indexer(store)
    await indexer.apply(restaurant(1))
    await indexer.apply(item(2, description="With green chillies."))  # spice 2
    await indexer.apply(
        item(3, menu_item_id="m-2", name="Plain Pulao", spice_level=0, description="Mild.")
    )
    await indexer.apply(
        item(4, menu_item_id="m-3", name="Beef Pulao", spice_level=0, description="Mild.")
    )
    await indexer.apply(
        restaurant(1, restaurant_id="r-3", name="Wok House", cuisine_type="Thai / Pan-Asian")
    )
    await indexer.apply(
        item(
            2,
            menu_item_id="m-7",
            restaurant_id="r-3",
            restaurant_name="Wok House",
            cuisine_type="Thai / Pan-Asian",
            name="Chicken Pulao Thai Style",
            spice_level=1,
        )
    )
    for rid, rating, count in (("r-1", 4.6, 20), ("r-3", 3.0, 2)):
        await store.set_restaurant_fields(
            ["restaurants"], rid, {"avg_rating": rating, "review_count": count}, 50
        )


async def test_assistant_filters_narrow_the_results(store: Store, embedder: FakeEmbedder) -> None:
    await _two_kitchens(store)
    r = Retriever(store._client, embedder)
    everything = {"m-1", "m-2", "m-3", "m-7"}
    assert await _ids(r, "chicken pulao biryani beef") == everything
    assert await _ids(r, "chicken pulao biryani beef", max_spice=0) == {"m-2", "m-3"}
    # "chillies" in the description is excluded by "chilli"; "beef" by name.
    assert await _ids(r, "chicken pulao biryani beef", exclude=("chilli", "Beef")) == {
        "m-2",
        "m-7",
    }
    assert await _ids(r, "chicken pulao biryani beef", cuisines=("thai",)) == {"m-7"}
    assert await _ids(r, "chicken pulao biryani beef", min_rating=4.0) == {"m-1", "m-2", "m-3"}
    assert await _ids(r, "chicken pulao biryani beef", min_rating=4.9) == set()
    assert await _ids(r, "wok", kind="restaurant", min_rating=4.0) == {"r-1"}
    assert await _ids(r, "house", kind="restaurant", cuisines=("pan-asian",)) == {"r-3"}


def _loads(table: dict[str, Load]):
    async def lookup(ids):
        return {rid: table[rid] for rid in ids if rid in table}

    return lookup


async def test_suggestions_weigh_rating_and_load(store: Store, embedder: FakeEmbedder) -> None:
    await _two_kitchens(store)
    idle = Load(active_orders=0, max_orders=10, accepting=True)
    r = Retriever(store._client, embedder, _loads({"r-1": idle, "r-3": idle}))
    hits = {h.id: h for h in await r.suggest_restaurants("chicken pulao", Filters(limit=5))}
    assert set(hits) == {"r-1", "r-3"}
    # Bayesian ratings: 4.6 by 20 → 4.38 → 0.950; 3.0 by 2 → 3.36 → 0.869.
    assert hits["r-1"].data["rating_factor"] == pytest.approx(0.950, abs=1e-3)
    assert hits["r-3"].data["rating_factor"] == pytest.approx(0.869, abs=1e-3)
    assert max(h.data["match"] for h in hits.values()) == 1.0
    for h in hits.values():  # an idle kitchen keeps its score
        assert h.score == pytest.approx(h.data["match"] * h.data["rating_factor"], abs=1e-3)
    top = hits["r-1"].data
    assert top["load"] == {"active_orders": 0, "max_orders": 10, "utilization": 0.0, "busy": False}
    assert {d["id"] for d in top["dishes"]} <= {"m-1", "m-2", "m-3"}
    assert "words" not in top and "text" not in top

    # The dish filters apply: nothing mild enough at r-1 that isn't beef...
    hits = await r.suggest_restaurants(
        "chicken pulao", Filters(max_spice=1, exclude=("beef", "plain"))
    )
    assert [h.id for h in hits] == ["r-3"]


async def test_full_kitchens_are_left_out_and_busy_ones_drop(
    store: Store, embedder: FakeEmbedder
) -> None:
    await _two_kitchens(store)
    full = Load(active_orders=10, max_orders=10, accepting=True)
    closed = Load(active_orders=0, max_orders=10, accepting=False)
    busy = Load(active_orders=9, max_orders=10, accepting=True)
    for load in (full, closed):
        r = Retriever(store._client, embedder, _loads({"r-1": load}))
        assert "r-1" not in {h.id for h in await r.suggest_restaurants("chicken", Filters())}

    calm = await Retriever(store._client, embedder).suggest_restaurants("chicken", Filters())
    hurried = await Retriever(store._client, embedder, _loads({"r-1": busy})).suggest_restaurants(
        "chicken", Filters()
    )
    score = {h.id: h.score for h in calm}
    busy_score = {h.id: h.score for h in hurried}
    assert busy_score["r-1"] == pytest.approx(score["r-1"] * 0.55)
    assert next(h for h in hurried if h.id == "r-1").data["load"]["busy"] is True
    # Unknown load (lookup failed): ranked as idle, and the hit says so.
    assert next(h for h in calm if h.id == "r-1").data["load"] is None


async def test_no_suggestions_when_no_restaurant_is_rated_high_enough(
    store: Store, embedder: FakeEmbedder
) -> None:
    await _two_kitchens(store)
    r = Retriever(store._client, embedder)
    assert await r.suggest_restaurants("chicken", Filters(min_rating=4.9)) == []
