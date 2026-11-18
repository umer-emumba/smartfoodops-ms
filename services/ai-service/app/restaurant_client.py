"""Live reads from restaurant-service's /internal API — what the index
can't be trusted for at the moment of answering:

- `loads`: each kitchen's active orders against its cap (restaurant
  suggestions, docs/week5-plan.md step 1). It changes with every order, so
  it isn't indexed.
- `quotes`: the current price, availability and stock of the dishes the
  assistant is about to show, and whether the restaurant takes orders (the
  check step, step 2, B5-D5) — the same endpoint order-service prices an
  order with, so a card shows what the order would cost.

Both are best-effort with short timeouts: any failure returns "unknown"
(an empty dict), never an error. Callers decide what unknown means —
suggestions rank without load; the check falls back to the index's copy."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sfo_common.middleware import propagate_correlation_id

from app import metrics
from app.config import settings
from app.retrieval import Load

logger = logging.getLogger(__name__)

client = httpx.AsyncClient(
    base_url=settings.restaurant_service_url,
    timeout=httpx.Timeout(settings.kitchen_load_timeout_seconds),
    event_hooks={"request": [propagate_correlation_id]},
)


async def loads(restaurant_ids: Sequence[str]) -> dict[str, Load]:
    if not restaurant_ids:
        return {}
    try:
        resp = await client.post(
            "/internal/restaurants/load", json={"restaurant_ids": list(restaurant_ids)}
        )
        resp.raise_for_status()
        rows = resp.json()["loads"]
        result = {
            str(row["restaurant_id"]): Load(
                active_orders=int(row["active_orders"]),
                max_orders=int(row["max_orders"]),
                accepting=bool(row["is_accepting_orders"]),
            )
            for row in rows
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        metrics.KITCHEN_LOAD_LOOKUPS.labels("failed").inc()
        logger.warning("Kitchen load lookup failed; ranking without it", exc_info=True)
        return {}
    metrics.KITCHEN_LOAD_LOOKUPS.labels("ok").inc()
    return result


@dataclass(frozen=True)
class LiveItem:
    price: Decimal
    orderable: bool  # available and (unlimited or some stock left)


@dataclass(frozen=True)
class LiveRestaurant:
    open: bool  # active and taking orders
    items: dict[str, LiveItem]  # only the dishes asked about that exist there


async def _quote(restaurant_id: str, item_ids: list[str]) -> LiveRestaurant | None:
    try:
        resp = await client.post(
            f"/internal/restaurants/{restaurant_id}/quote", json={"item_ids": item_ids}
        )
        if resp.status_code == 404:
            return LiveRestaurant(open=False, items={})
        resp.raise_for_status()
        body = resp.json()
        r = body["restaurant"]
        return LiveRestaurant(
            open=r["status"] == "active" and bool(r["is_accepting_orders"]),
            items={
                str(i["id"]): LiveItem(
                    price=Decimal(str(i["price"])),
                    orderable=bool(i["is_available"])
                    and (i["stock_count"] is None or int(i["stock_count"]) > 0),
                )
                for i in body["items"]
            },
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError, ArithmeticError):
        logger.warning("Live quote failed for %s; using the index", restaurant_id, exc_info=True)
        return None


async def quotes(items_by_restaurant: dict[str, list[str]]) -> dict[str, LiveRestaurant]:
    """Restaurant ID → its live state, one quote per restaurant, in
    parallel. A restaurant whose quote failed is missing from the result."""
    ids = list(items_by_restaurant)
    results = await asyncio.gather(*(_quote(rid, items_by_restaurant[rid]) for rid in ids))
    found = {rid: live for rid, live in zip(ids, results, strict=True) if live is not None}
    metrics.LIVE_QUOTES.labels("ok").inc(len(found))
    metrics.LIVE_QUOTES.labels("failed").inc(len(ids) - len(found))
    return found
