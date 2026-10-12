from fastapi import APIRouter, Depends
from sfo_common.errors import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from app import owners, repository
from app.db import get_session
from app.schemas import QuoteRequest, QuoteResponse

# Service-to-service endpoints. api-gateway only forwards /v1/* paths, so
# nothing under /internal is reachable by clients — only by other services on
# the internal network.
router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/restaurants/{restaurant_id}/quote", response_model=QuoteResponse)
async def quote(
    restaurant_id: str,
    body: QuoteRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """order-service's single lookup at order creation: the restaurant's
    status plus current price/availability/stock for just the ordered items.

    Unlike the public menu endpoints, an inactive restaurant isn't 404'd — its
    status is returned and order-service applies the visibility rule. Item IDs
    that don't belong to this restaurant are omitted, not errors.
    """
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    items = await repository.get_items_by_ids(session, restaurant_id, body.item_ids)
    return {"restaurant": restaurant, "items": items}


@router.get("/restaurants/{restaurant_id}/owner")
async def restaurant_owner(
    restaurant_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """tracking-service's fallback when `restaurant:{id}:owner` is missing
    from Redis: returns the owner and writes the key again."""
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    await owners.record(restaurant.id, restaurant.admin_user_id)
    return {"restaurant_id": restaurant.id, "admin_user_id": restaurant.admin_user_id}
