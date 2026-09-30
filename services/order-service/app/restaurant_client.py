from dataclasses import dataclass
from decimal import Decimal

import httpx
from sfo_common.errors import NotFoundError, ServiceUnavailableError

from app.config import settings

# Sync REST call to restaurant-service — the source of truth for menu prices,
# availability and restaurant status. Called anonymously on purpose: the
# public read endpoints already 404 any restaurant that isn't `active`, so a
# pending/suspended restaurant is rejected without order-service needing its
# own copy of that visibility rule. Week 2's saga replaces this with a
# Temporal activity (retried by Temporal); Week 1 has no retry, just a
# bounded timeout and a clean 503 when restaurant-service is unreachable.
client = httpx.AsyncClient(
    base_url=settings.restaurant_service_url, timeout=httpx.Timeout(5.0, connect=2.0)
)


@dataclass(frozen=True)
class RestaurantSnapshot:
    id: str
    status: str
    is_accepting_orders: bool


@dataclass(frozen=True)
class MenuItemSnapshot:
    id: str
    name: str
    price: Decimal
    is_available: bool
    stock_count: int | None


async def _get_json(path: str) -> dict | list:
    try:
        resp = await client.get(path)
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("restaurant-service is unreachable") from exc
    if resp.status_code == 404:
        raise NotFoundError("Restaurant not found")
    if resp.status_code >= 400:
        raise ServiceUnavailableError(
            "restaurant-service returned an error", details={"status": resp.status_code}
        )
    return resp.json()


async def get_restaurant(restaurant_id: str) -> RestaurantSnapshot:
    data = await _get_json(f"/v1/restaurants/{restaurant_id}")
    return RestaurantSnapshot(
        id=data["id"], status=data["status"], is_accepting_orders=data["is_accepting_orders"]
    )


async def get_menu(restaurant_id: str) -> dict[str, MenuItemSnapshot]:
    data = await _get_json(f"/v1/restaurants/{restaurant_id}/menu")
    return {
        item["id"]: MenuItemSnapshot(
            id=item["id"],
            name=item["name"],
            # str() first — Decimal(float) would carry the float's binary
            # rounding error (8.5 is exact, but 0.1 isn't) into the order total.
            price=Decimal(str(item["price"])),
            is_available=item["is_available"],
            stock_count=item["stock_count"],
        )
        for item in data
    }
