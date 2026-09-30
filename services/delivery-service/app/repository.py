from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rider


async def get_rider_by_user_id(session: AsyncSession, user_id: str) -> Rider | None:
    result = await session.execute(select(Rider).where(Rider.user_id == user_id))
    return result.scalar_one_or_none()


async def create_rider(session: AsyncSession, rider: Rider) -> Rider:
    session.add(rider)
    await session.flush()
    return rider


async def get_rider(session: AsyncSession, rider_id: str) -> Rider | None:
    return await session.get(Rider, rider_id)


async def list_riders(
    session: AsyncSession, *, verification_status: str | None, limit: int
) -> list[Rider]:
    """system_admin management view — all riders, optionally filtered by
    verification_status, unlike the rider's own self-service /riders/me."""
    stmt = select(Rider).order_by(Rider.created_at.desc())
    if verification_status:
        stmt = stmt.where(Rider.verification_status == verification_status)
    result = await session.execute(stmt.limit(limit))
    return list(result.scalars().all())
