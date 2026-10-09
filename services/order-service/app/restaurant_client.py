from dataclasses import dataclass
from decimal import Decimal

import httpx
from sfo_common.errors import NotFoundError, ServiceUnavailableError
from sfo_common.middleware import propagate_correlation_id

from app.config import settings

# Sync REST call to restaurant-service — the source of truth for menu prices,
# availability and restaurant status. Uses restaurant-service's /internal
# quote endpoint (one round trip, only the ordered items), which api-gateway
# never routes to — it only forwards /v1/*.
# Week 2's saga replaces this with a
# Temporal activity (retried by Temporal); Week 1 has no retry, just a
# bounded timeout and a clean 503 when restaurant-service is unreachable.
# The request hook forwards X-Request-ID, so restaurant-service logs this call
# under the same trace ID as the order request that triggered it.
client = httpx.AsyncClient(
    base_url=settings.restaurant_service_url,
    timeout=httpx.Timeout(5.0, connect=2.0),
    event_hooks={"request": [propagate_correlation_id]},
)


@dataclass(frozen=True)
class RestaurantSnapshot:
    id: str
    status: str
    is_accepting_orders: bool
    admin_user_id: str
    lat: float
    lng: float


@dataclass(frozen=True)
class MenuItemSnapshot:
    id: str
    name: str
    price: Decimal
    is_available: bool
    stock_count: int | None


async def get_quote(
    restaurant_id: str, item_ids: list[str]
) -> tuple[RestaurantSnapshot, dict[str, MenuItemSnapshot]]:
    """One round trip for the restaurant's status and just the requested items
    — not the full public menu. Items that aren't on this restaurant's menu are
    simply absent from the result. The restaurant's status is returned as-is
    (not 404'd when inactive), so the caller applies the visibility rule."""
    try:
        resp = await client.post(
            f"/internal/restaurants/{restaurant_id}/quote", json={"item_ids": item_ids}
        )
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("restaurant-service is unreachable") from exc
    if resp.status_code == 404:
        raise NotFoundError("Restaurant not found")
    if resp.status_code >= 400:
        raise ServiceUnavailableError(
            "restaurant-service returned an error", details={"status": resp.status_code}
        )
    data = resp.json()
    r = data["restaurant"]
    restaurant = RestaurantSnapshot(
        id=r["id"],
        status=r["status"],
        is_accepting_orders=r["is_accepting_orders"],
        admin_user_id=r["admin_user_id"],
        lat=r["lat"],
        lng=r["lng"],
    )
    items = {
        item["id"]: MenuItemSnapshot(
            id=item["id"],
            name=item["name"],
            # str() first — Decimal(float) would carry the float's binary
            # rounding error (8.5 is exact, but 0.1 isn't) into the order total.
            price=Decimal(str(item["price"])),
            is_available=item["is_available"],
            stock_count=item["stock_count"],
        )
        for item in data["items"]
    }
    return restaurant, items
