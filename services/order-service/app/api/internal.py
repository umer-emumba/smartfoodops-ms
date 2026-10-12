from fastapi import APIRouter, Depends
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
