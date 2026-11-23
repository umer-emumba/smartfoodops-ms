"""Retrieval against a real Qdrant (fake embeddings, so ranking here is
BM25's and the point is the filters): what a search may never return, and
that each filter narrows the results as asked."""

from decimal import Decimal

import pytest
from app.indexer import Indexer
from app.retrieval import Filters, Retriever
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
