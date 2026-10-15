from fastapi import APIRouter, Depends, Query
from sfo_auth import CurrentUser
from sfo_common.errors import NotFoundError
from sfo_common.pagination import Page
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user
from app.db import get_session
from app.models import Notification
from app.schemas import NotificationResponse

router = APIRouter(prefix="/v1", tags=["notifications"])


@router.get("/notifications", response_model=Page[NotificationResponse])
async def list_my_notifications(
    unread: bool = Query(default=False, description="Only notifications not yet marked read"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's in-app notifications, newest first."""
    return await repository.list_for_user(
        session, current.id, unread=unread, cursor=cursor, limit=limit
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: str,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Notification:
    """Idempotent. 404 for someone else's notification, as for a missing one."""
    notification = await repository.mark_read(session, current.id, notification_id)
    if notification is None:
        raise NotFoundError("Notification not found")
    return notification
