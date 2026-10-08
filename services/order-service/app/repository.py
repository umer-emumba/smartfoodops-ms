from sfo_common.pagination import build_page, paginate_newest_first
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Order


async def create_order(session: AsyncSession, order: Order) -> Order:
    session.add(order)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: str) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    )
    return result.scalar_one_or_none()


async def get_order_by_idempotency_key(
    session: AsyncSession, customer_id: str, idempotency_key: str
) -> Order | None:
    result = await session.execute(
        select(Order)
        .where(Order.customer_id == customer_id, Order.idempotency_key == idempotency_key)
        .options(selectinload(Order.items))
    )
    return result.scalar_one_or_none()


async def list_orders_admin(
    session: AsyncSession,
    *,
    status: str | None,
    restaurant_id: str | None,
    customer_id: str | None,
    cursor: str | None,
    limit: int,
) -> dict:
    stmt = select(Order).options(selectinload(Order.items))
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if restaurant_id is not None:
        stmt = stmt.where(Order.restaurant_id == restaurant_id)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    result = await session.execute(paginate_newest_first(stmt, Order, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)


async def list_orders_for_customer(
    session: AsyncSession, customer_id: str, *, cursor: str | None, limit: int
) -> dict:
    stmt = select(Order).where(Order.customer_id == customer_id).options(selectinload(Order.items))
    result = await session.execute(paginate_newest_first(stmt, Order, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)
