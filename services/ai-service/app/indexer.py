"""Keeps the search index in step with the catalog and order history
(docs/week4-plan.md step 3, B4-D4, B4-D5): each event becomes Qdrant writes.
The live consumer (app/consumer.py) and the backfill (app/reindex.py) both
go through here, so they build identical documents.

Ordering and repeats are handled by versions, not by remembering events
(no processed-events table): catalog writes only land if newer
(app/store.py), orders are insert-only, and the customer summary is rebuilt
from scratch. Replaying a whole topic leaves the index as it was.

Orders whose restaurant isn't indexed are skipped: without it the document
has no name or cuisine, and these are orders of restaurants that no longer
exist (deleted dev data)."""

import logging
from collections.abc import Iterable, Sequence

from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sfo_events import DomainEvent, EventType
from sfo_events.payloads import (
    MenuItemDeletedPayload,
    MenuItemUpsertedPayload,
    OrderTransitionPayload,
    RestaurantStatusChangedPayload,
    RestaurantUpsertedPayload,
    ReviewSubmittedPayload,
)

from app import documents
from app.documents import DeliveredOrder
from app.store import CATALOG, MENU_ITEMS, ORDER_HISTORY, RESTAURANTS, REVIEWS, Store

logger = logging.getLogger(__name__)


class QdrantUnavailable(ConnectionError):
    """Qdrant unreachable or failing (5xx). A ConnectionError, so the
    consumer treats it as an outage: rewind and wait, never the DLQ (same rule
    as W3-D5 — an outage isn't a poison message)."""


class Indexer:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def apply(self, event: DomainEvent) -> None:
        try:
            await self.store.ensure_collections()
            await self._apply(event)
        except ResponseHandlingException as exc:
            raise QdrantUnavailable(str(exc)) from exc
        except UnexpectedResponse as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise QdrantUnavailable(str(exc)) from exc
            raise

    async def _apply(self, event: DomainEvent) -> None:
        p = event.typed_payload()
        match p:
            case RestaurantUpsertedPayload():
                await self.restaurant(p)
            case RestaurantStatusChangedPayload():
                await self.store.set_restaurant_fields(
                    CATALOG, p.restaurant_id, {"restaurant_status": p.to_status}, p.source_version
                )
            case MenuItemUpsertedPayload():
                await self.menu_items([p], p.source_version)
            case MenuItemDeletedPayload():
                await self.store.upsert_newer(
                    MENU_ITEMS,
                    [
                        documents.menu_item_tombstone(
                            p.menu_item_id, p.restaurant_id, p.source_version
                        )
                    ],
                    p.source_version,
                )
            case ReviewSubmittedPayload():
                await self.reviews([p], p.restaurant_id, p.source_version)
                await self.store.set_restaurant_fields(
                    [RESTAURANTS],
                    p.restaurant_id,
                    {"avg_rating": p.avg_rating, "review_count": p.review_count},
                    p.source_version,
                )
            case OrderTransitionPayload() if event.event_type == EventType.ORDER_DELIVERED:
                if p.items is None:
                    return  # delivered before OrderDelivered carried its items
                await self.orders(
                    [
                        DeliveredOrder(
                            order_id=p.order_id,
                            customer_id=p.customer_id,
                            restaurant_id=p.restaurant_id,
                            total_amount=float(p.total_amount),
                            delivered_at=event.occurred_at,
                            items=p.items,
                        )
                    ]
                )
            case _:
                return  # other order events don't change the index

    async def restaurant(self, p: RestaurantUpsertedPayload) -> None:
        await self.store.upsert_newer(RESTAURANTS, [documents.restaurant(p)], p.source_version)

    async def menu_items(self, items: Sequence[MenuItemUpsertedPayload], version: int) -> None:
        await self.store.upsert_newer(MENU_ITEMS, [documents.menu_item(p) for p in items], version)

    async def reviews(
        self, reviews: Sequence[ReviewSubmittedPayload], restaurant_id: str, version: int
    ) -> None:
        """Reviews of one restaurant; its indexed point supplies city and
        location for the review's filters."""
        where = await self.store.payload(
            RESTAURANTS, documents.point_id("restaurant", restaurant_id)
        )
        docs = [doc for r in reviews for doc in documents.review(r, where)]
        await self.store.upsert_newer(REVIEWS, docs, version)

    async def orders(self, orders: Iterable[DeliveredOrder]) -> None:
        """Indexes new delivered orders, then rebuilds the summary of each
        customer who got one."""
        restaurants: dict[str, dict | None] = {}
        docs = []
        customers = set()
        for o in orders:
            if o.restaurant_id not in restaurants:
                restaurants[o.restaurant_id] = await self.store.payload(
                    RESTAURANTS, documents.point_id("restaurant", o.restaurant_id)
                )
            where = restaurants[o.restaurant_id]
            if where is None:
                logger.info(
                    "Order %s skipped: restaurant %s not indexed", o.order_id, o.restaurant_id
                )
                continue
            docs.append(documents.order(o, where))
            customers.add(o.customer_id)
        if await self.store.insert_new(ORDER_HISTORY, docs):
            for customer_id in sorted(customers):
                await self.customer_summary(customer_id)

    async def customer_summary(self, customer_id: str) -> None:
        orders = await self.store.customer_orders(customer_id)
        if orders:
            await self.store.upsert(
                ORDER_HISTORY, [documents.customer_summary(customer_id, orders)]
            )
