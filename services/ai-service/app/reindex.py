"""Rebuilds the search index from the source services — after the first
deploy, or when the embedding model or document format changes (a new
`settings.index_suffix`, i.e. new collections):

    docker compose exec ai-service python -m app.reindex

Reads restaurant-service's catalog export and order-service's delivered
orders (internal endpoints), and writes them through the same indexer as
live events. Safe while serving and while events flow: catalog writes only
land if newer, orders are insert-only (app/store.py) — so a reindex never
moves the index backwards, and running it twice changes nothing."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
from sfo_events.payloads import (
    MenuItemUpsertedPayload,
    OrderLine,
    RestaurantUpsertedPayload,
    ReviewSubmittedPayload,
)
from sfo_observability import configure_logging

from app import qdrant
from app.config import settings
from app.documents import DeliveredOrder
from app.indexer import Indexer
from app.store import default_store

logger = logging.getLogger(__name__)


async def _pages(client: httpx.AsyncClient, url: str, limit: int) -> AsyncIterator[list[dict]]:
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        page = resp.json()
        yield page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return


async def reindex(indexer: Indexer, client: httpx.AsyncClient) -> dict[str, int]:
    await indexer.store.ensure_collections()
    counts = {"restaurants": 0, "menu_items": 0, "reviews": 0, "orders": 0}
    url = f"{settings.restaurant_service_url}/internal/catalog/export"
    async for entries in _pages(client, url, 10):
        for entry in entries:
            restaurant = RestaurantUpsertedPayload.model_validate(entry["restaurant"])
            version = restaurant.source_version
            await indexer.restaurant(restaurant)
            items = [MenuItemUpsertedPayload.model_validate(i) for i in entry["menu_items"]]
            await indexer.menu_items(items, version)
            reviews = [ReviewSubmittedPayload.model_validate(r) for r in entry["reviews"]]
            await indexer.reviews(reviews, restaurant.restaurant_id, version)
            counts["restaurants"] += 1
            counts["menu_items"] += len(items)
            counts["reviews"] += len(reviews)
        logger.info("Catalog so far: %s", counts)

    # After the catalog: an order's document needs its restaurant indexed.
    url = f"{settings.order_service_url}/internal/orders/delivered"
    async for orders in _pages(client, url, 200):
        await indexer.orders(
            DeliveredOrder(
                order_id=o["order_id"],
                customer_id=o["customer_id"],
                restaurant_id=o["restaurant_id"],
                total_amount=float(o["total_amount"]),
                delivered_at=datetime.fromisoformat(o["delivered_at"]),
                items=[OrderLine.model_validate(line) for line in o["items"]],
            )
            for o in orders
        )
        counts["orders"] += len(orders)
    return counts


async def _run() -> int:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            counts = await reindex(Indexer(default_store()), client)
    finally:
        await qdrant.client.close()
    print(f"Reindexed {counts} into *__{settings.index_suffix}")
    return 0


if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    sys.exit(asyncio.run(_run()))
