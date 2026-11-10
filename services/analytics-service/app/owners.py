"""Who owns a restaurant, for the restaurant-admin check on
`GET /v1/analytics/restaurants/{id}`.

The order facts carry the owner snapshotted at order time, so a restaurant
with orders by this admin needs no call. Otherwise (a new restaurant with no
orders yet, or orders that all name someone else) restaurant-service is
asked: it holds the real owner. Same endpoint tracking-service uses."""

import httpx
from sfo_common.errors import ServiceUnavailableError
from sfo_common.middleware import propagate_correlation_id

from app.config import settings

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(3.0, connect=1.0),
    event_hooks={"request": [propagate_correlation_id]},
)


async def restaurant_owner(restaurant_id: str) -> str | None:
    """The owning admin's user ID, or None if there's no such restaurant."""
    url = f"{settings.restaurant_service_url}/internal/restaurants/{restaurant_id}/owner"
    try:
        resp = await _client.get(url)
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("Restaurant ownership is temporarily unavailable") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ServiceUnavailableError("Restaurant ownership is temporarily unavailable")
    return str(resp.json()["admin_user_id"])
