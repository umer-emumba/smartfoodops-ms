from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment


async def get_payment_by_order_id(session: AsyncSession, order_id: str) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.order_id == order_id))
    return result.scalar_one_or_none()
