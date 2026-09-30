from fastapi import APIRouter, Depends, Query
from sfo_auth import CurrentUser
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user
from app.db import get_session
from app.models import Notification
from app.schemas import NotificationResponse

router = APIRouter(prefix="/v1", tags=["notifications"])

# Week 1: read-only skeleton. The Kafka consumer + Celery dispatch task that
# populate this table land in Week 3 — see docs/PROGRESS.md.


@router.get("/notifications", response_model=list[NotificationResponse])
async def list_my_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Notification]:
    return await repository.list_for_user(session, current.id, limit=limit)
