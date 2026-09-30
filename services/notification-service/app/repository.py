from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


async def list_for_user(session: AsyncSession, user_id: str, *, limit: int) -> list[Notification]:
    result = await session.execute(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
