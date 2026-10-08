from sfo_common.pagination import build_page, paginate_newest_first
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


async def list_for_user(
    session: AsyncSession, user_id: str, *, cursor: str | None, limit: int
) -> dict:
    stmt = select(Notification).where(Notification.user_id == user_id)
    result = await session.execute(
        paginate_newest_first(stmt, Notification, cursor=cursor, limit=limit)
    )
    return build_page(list(result.scalars().all()), limit)
