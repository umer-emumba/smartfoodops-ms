from datetime import UTC, datetime

from sfo_common.pagination import build_page, paginate_newest_first
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification

# The inbox is in-app notifications; email/SMS rows are delivery records of
# the same messages.
_INBOX = Notification.channel == "in_app"


async def list_for_user(
    session: AsyncSession, user_id: str, *, unread: bool, cursor: str | None, limit: int
) -> dict:
    stmt = select(Notification).where(Notification.user_id == user_id, _INBOX)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    result = await session.execute(
        paginate_newest_first(stmt, Notification, cursor=cursor, limit=limit)
    )
    return build_page(list(result.scalars().all()), limit)


async def mark_read(
    session: AsyncSession, user_id: str, notification_id: str
) -> Notification | None:
    """None if it isn't this user's in-app notification. Already read →
    unchanged (the first read time stays)."""
    notification = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id, Notification.user_id == user_id, _INBOX
            )
        )
    ).scalar_one_or_none()
    if notification is not None and notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
    return notification
