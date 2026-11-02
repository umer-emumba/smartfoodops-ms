"""The indexer against a real Qdrant (fake embeddings): versions decide what
lands, deletes are tombstones, status and rating changes reach every point,
orders need their restaurant, and outages are retried, not dead-lettered."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from app import documents
from app.documents import point_id
from app.indexer import Indexer, QdrantUnavailable
from app.store import MENU_ITEMS, ORDER_HISTORY, RESTAURANTS, REVIEWS, Store
from sfo_events import DomainEvent, EventType, validate_payload
from sfo_events.consumer import is_transient

from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.db

RID = "r-1"
AT = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def _event(event_type: EventType, payload: dict[str, Any], n: int = 0) -> DomainEvent:
    return DomainEvent(
        event_id=f"e-{event_type}-{n}",
        event_type=event_type,
        aggregate_id=payload.get("restaurant_id") or payload["order_id"],
        occurred_at=AT,
        payload=validate_payload(event_type, payload),
    )


def restaurant(version: int, **over: Any) -> DomainEvent:
    return _event(
        EventType.RESTAURANT_UPSERTED,
        {
            "restaurant_id": RID,
            "name": "Desi Dastarkhwan",
            "description": "Dum biryani in F-7.",
            "city": "Islamabad",
            "address": "Jinnah Super, F-7",
            "lat": 33.72,
            "lng": 73.05,
            "cuisine_type": "Pakistani",
            "status": "active",
            "is_accepting_orders": True,
            "avg_prep_time_minutes": 25,
            "avg_rating": None,
            "review_count": 0,
            "source_version": version,
            **over,
        },
        version,
    )


def item(version: int, **over: Any) -> DomainEvent:
    return _event(
        EventType.MENU_ITEM_UPSERTED,
        {
            "menu_item_id": "m-1",
            "restaurant_id": RID,
            "restaurant_name": "Desi Dastarkhwan",
            "restaurant_status": "active",
            "cuisine_type": "Pakistani",
            "city": "Islamabad",
            "lat": 33.72,
            "lng": 73.05,
            "category_id": "c-1",
            "category_name": "Rice",
            "name": "Chicken Biryani",
            "description": "Dum-cooked.",
            "price": Decimal("690"),
            "dietary_tags": ["halal"],
            "spice_level": 2,
            "calories": None,
            "is_available": True,
            "in_stock": True,
            "source_version": version,
            **over,
        },
        version,
    )


def delivered(order_id: str, restaurant_id: str = RID) -> DomainEvent:
    return _event(
        EventType.ORDER_DELIVERED,
        {
            "order_id": order_id,
            "order_number": 1000,
            "customer_id": "u-1",
            "restaurant_id": restaurant_id,
            "restaurant_admin_id": "u-owner",
            "total_amount": Decimal("1500"),
            "from_status": "picked_up",
            "to_status": "delivered",
            "items": [
                {
                    "menu_item_id": "m-1",
                    "name": "Chicken Biryani",
                    "quantity": 2,
                    "unit_price": "690",
                }
            ],
        },
    )


async def _payload(store: Store, kind: str, key: str, entity: str) -> dict | None:
    return await store.payload(kind, point_id(entity, key))


async def test_only_newer_versions_land(store: Store, embedder: FakeEmbedder) -> None:
    indexer = Indexer(store)
    await indexer.apply(item(5, price=Decimal("700")))
    await indexer.apply(item(3, price=Decimal("500")))  # older: ignored
    await indexer.apply(item(5, price=Decimal("999")))  # same: ignored (redelivery)
    stored = await _payload(store, MENU_ITEMS, "m-1", "menu_item")
    assert stored is not None
    assert (stored["price"], stored["source_version"]) == (700.0, 5)
    assert len(embedder.embedded) == 1  # stale events aren't even embedded

    await indexer.apply(item(6, price=Decimal("720")))
    stored = await _payload(store, MENU_ITEMS, "m-1", "menu_item")
    assert stored is not None and stored["price"] == 720.0


async def test_delete_is_a_tombstone(store: Store) -> None:
    indexer = Indexer(store)
    await indexer.apply(item(2))
    deleted = {"menu_item_id": "m-1", "restaurant_id": RID, "source_version": 3}
    await indexer.apply(_event(EventType.MENU_ITEM_DELETED, deleted))
    await indexer.apply(item(2))  # a late, older upsert can't bring it back
    stored = await _payload(store, MENU_ITEMS, "m-1", "menu_item")
    assert stored is not None
    assert stored["deleted"] is True and "name" not in stored


async def test_status_change_reaches_every_older_point(store: Store) -> None:
    indexer = Indexer(store)
    await indexer.apply(restaurant(1))
    await indexer.apply(item(2))
    review = {
        "order_id": "o-1",
        "restaurant_id": RID,
        "restaurant_name": "Desi Dastarkhwan",
        "rating": 4,
        "text": "Proper dum biryani",
        "item_ids": ["m-1"],
        "created_at": AT,
        "avg_rating": 4.0,
        "review_count": 1,
        "source_version": 3,
    }
    await indexer.apply(_event(EventType.REVIEW_SUBMITTED, review))
    suspended = {
        "restaurant_id": RID,
        "from_status": "active",
        "to_status": "suspended",
        "source_version": 4,
    }
    await indexer.apply(_event(EventType.RESTAURANT_STATUS_CHANGED, suspended))

    for kind, key, entity in [
        (RESTAURANTS, RID, "restaurant"),
        (MENU_ITEMS, "m-1", "menu_item"),
        (REVIEWS, "o-1:0", "review"),
    ]:
        stored = await _payload(store, kind, key, entity)
        assert stored is not None
        assert (stored["restaurant_status"], stored["source_version"]) == ("suspended", 4), kind
    r = await _payload(store, RESTAURANTS, RID, "restaurant")
    assert r is not None and (r["avg_rating"], r["review_count"]) == (4.0, 1)
    rv = await _payload(store, REVIEWS, "o-1:0", "review")
    assert rv is not None and rv["city"] == "Islamabad"  # from the restaurant's point


async def test_orders_need_their_restaurant_and_build_a_summary(store: Store) -> None:
    indexer = Indexer(store)
    await indexer.apply(delivered("o-orphan", restaurant_id="r-gone"))
    assert await _payload(store, ORDER_HISTORY, "o-orphan", "order") is None

    await indexer.apply(restaurant(1))
    await indexer.apply(delivered("o-1"))
    await indexer.apply(delivered("o-1"))  # redelivered: insert-only
    await indexer.apply(delivered("o-2"))
    order = await _payload(store, ORDER_HISTORY, "o-1", "order")
    assert order is not None and order["restaurant_name"] == "Desi Dastarkhwan"
    summary = await _payload(store, ORDER_HISTORY, "u-1", "customer_summary")
    assert summary is not None
    assert summary["order_count"] == 2
    assert summary["top_dishes"] == ["Chicken Biryani"]
    assert "Chicken Biryani (4)" in summary["text"]


async def test_qdrant_outage_is_transient(embedder: FakeEmbedder) -> None:
    from qdrant_client import AsyncQdrantClient

    dead = AsyncQdrantClient(url="http://localhost:1", timeout=1)
    with pytest.raises(QdrantUnavailable) as caught:
        await Indexer(Store(dead, embedder)).apply(item(1))
    assert is_transient(caught.value)
    await dead.close()


def test_point_ids_are_stable() -> None:
    assert documents.point_id("menu_item", "m-1") == documents.point_id("menu_item", "m-1")
    assert documents.point_id("menu_item", "m-1") != documents.point_id("restaurant", "m-1")
