from dataclasses import dataclass

import httpx
from sfo_common.errors import ServiceUnavailableError
from sfo_common.middleware import propagate_correlation_id

from app.config import settings

# Sync REST call to order-service's /internal summary — the source of truth
# for whether an order was delivered, whose it is and what was on it. Used
# only to check a review's eligibility. No retry: a bounded timeout and a
# clean 503, and the customer just submits the review again. The request hook
# forwards X-Request-ID so both services log the call under one ID.
client = httpx.AsyncClient(
    base_url=settings.order_service_url,
    timeout=httpx.Timeout(3.0, connect=1.0),
    event_hooks={"request": [propagate_correlation_id]},
)


@dataclass(frozen=True)
class OrderSummary:
    id: str
    customer_id: str
    restaurant_id: str
    status: str
    menu_item_ids: list[str]


async def get_order(order_id: str) -> OrderSummary | None:
    """None when order-service has no such order."""
    try:
        resp = await client.get(f"/internal/orders/{order_id}/summary")
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("order-service is unreachable") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ServiceUnavailableError(
            "order-service returned an error", details={"status": resp.status_code}
        )
    data = resp.json()
    return OrderSummary(
        id=data["id"],
        customer_id=data["customer_id"],
        restaurant_id=data["restaurant_id"],
        status=data["status"],
        menu_item_ids=list(data["menu_item_ids"]),
    )
