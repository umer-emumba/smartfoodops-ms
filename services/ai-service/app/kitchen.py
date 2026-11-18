"""Kitchen load for restaurant suggestions (docs/week5-plan.md step 1):
restaurant-service's POST /internal/restaurants/load, one call per
suggestion request.

Load isn't in the index — it changes with every order — so it's read live.
It's a ranking hint, not a gate: a short timeout, and any failure is "load
unknown" (an empty dict), never a failed suggestion. Ordering itself checks
capacity atomically in restaurant-service."""

import logging
from collections.abc import Sequence

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
