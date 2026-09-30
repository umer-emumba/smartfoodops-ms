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
    limit: int,
) -> list[Order]:
    stmt = select(Order).order_by(Order.created_at.desc()).options(selectinload(Order.items))
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if restaurant_id is not None:
        stmt = stmt.where(Order.restaurant_id == restaurant_id)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    result = await session.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def list_orders_for_customer(
    session: AsyncSession, customer_id: str, *, limit: int
) -> list[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .options(selectinload(Order.items))
    )
    return list(result.scalars().all())
