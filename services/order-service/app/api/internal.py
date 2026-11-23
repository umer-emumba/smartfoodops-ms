from fastapi import APIRouter, Depends, Query
from sfo_common.errors import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository, tracking
from app.db import get_session

# Service-to-service endpoints. api-gateway only forwards /v1/* paths, so
# nothing under /internal is reachable by clients.
router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/orders/{order_id}/tracking")
async def order_tracking_state(order_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """tracking-service's fallback when an order's Redis state key is
    missing: rebuilds the key from the order row and returns it."""
    order = await repository.get_order(session, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    return await tracking.write_state(order)


@router.get("/orders/{order_id}/summary")
async def order_summary(order_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """restaurant-service's review eligibility check: who placed the order,
    where from, whether it was delivered, and which menu items were on it."""
    order = await repository.get_order(session, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "restaurant_id": order.restaurant_id,
        "status": order.status,
        "delivered_at": order.delivered_at,
        "menu_item_ids": list(dict.fromkeys(item.menu_item_id for item in order.items)),
    }


@router.get("/orders/delivered")
async def delivered_orders(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """ai-service's order-history backfill (`python -m app.reindex`): every
    delivered order with its items, in pages. The live path is OrderDelivered,
    which carries the same fields."""
    page = await repository.list_delivered(session, cursor=cursor, limit=limit)
    return {
        "items": [
            {
                "order_id": order.id,
                "customer_id": order.customer_id,
                "restaurant_id": order.restaurant_id,
                "total_amount": order.total_amount,
                "delivered_at": order.delivered_at,
                "items": repository.order_lines(order),
            }
            for order in page["items"]
        ],
        "next_cursor": page["next_cursor"],
    }
