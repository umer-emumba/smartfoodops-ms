from fastapi import APIRouter, Depends, Query
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ForbiddenError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user, require_roles
from app.db import get_session
from app.models import Order, OrderItem
from app.schemas import OrderCreate, OrderResponse

router = APIRouter(prefix="/v1", tags=["orders"])

# Week 1: basic order creation only, no saga yet — see docs/PROGRESS.md.
# Week 2 adds the Temporal OrderSagaWorkflow that drives status transitions.


@router.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    session: AsyncSession = Depends(get_session),
) -> Order:
    subtotal = sum(item.quantity * item.unit_price for item in body.items)
    # Tax/delivery-fee/discount computation is the saga's "price/discount"
    # step starting Week 2 — Week 1 just establishes the fields (subtotal only).
    order = Order(
        customer_id=current.id,
        restaurant_id=body.restaurant_id,
        delivery_address_text=body.delivery_address_text,
        delivery_lat=body.delivery_lat,
        delivery_lng=body.delivery_lng,
        special_instructions=body.special_instructions,
        promo_code=body.promo_code,
        subtotal=subtotal,
        total_amount=subtotal,
    )
    order.items = [
        OrderItem(
            menu_item_id=i.menu_item_id,
            item_name_snapshot=i.item_name,
            quantity=i.quantity,
            unit_price=i.unit_price,
            special_instructions=i.special_instructions,
        )
        for i in body.items
    ]
    order = await repository.create_order(session, order)
    await session.commit()
    return order


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Order:
    order = await repository.get_order(session, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    if order.customer_id != current.id and current.role != Role.SYSTEM_ADMIN:
        raise ForbiddenError("Not your order")
    return order


@router.get("/orders", response_model=list[OrderResponse])
async def list_my_orders(
    limit: int = Query(default=20, le=100),
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    session: AsyncSession = Depends(get_session),
) -> list[Order]:
    return await repository.list_orders_for_customer(session, current.id, limit=limit)
