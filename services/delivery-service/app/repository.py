from sfo_common.pagination import build_page, paginate_newest_first
from sfo_events import EventType, new_outbox_row
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    RIDER_STATUS_OFFLINE,
    Delivery,
    DeliveryOffer,
    OutboxEvent,
    Rider,
    RiderVerificationHistory,
)
from app.state_machine import WITH_RIDER


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
    session: AsyncSession, *, verification_status: str | None, cursor: str | None, limit: int
) -> dict:
    """system_admin management view — all riders, optionally filtered by
    verification_status, unlike the rider's own self-service /riders/me."""
    stmt = select(Rider)
    if verification_status:
        stmt = stmt.where(Rider.verification_status == verification_status)
    result = await session.execute(paginate_newest_first(stmt, Rider, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)


async def list_verification_history(
    session: AsyncSession, rider_id: str
) -> list[RiderVerificationHistory]:
    result = await session.execute(
        select(RiderVerificationHistory)
        .where(RiderVerificationHistory.rider_id == rider_id)
        .order_by(RiderVerificationHistory.created_at)
    )
    return list(result.scalars().all())


async def get_active_delivery_for_rider(session: AsyncSession, rider_id: str) -> Delivery | None:
    """The delivery a rider currently holds (offered, assigned or picked up)."""
    result = await session.execute(
        select(Delivery).where(Delivery.rider_id == rider_id, Delivery.status.in_(WITH_RIDER))
    )
    return result.scalars().first()


async def get_pending_offer(session: AsyncSession, rider_id: str) -> DeliveryOffer | None:
    result = await session.execute(
        select(DeliveryOffer).where(
            DeliveryOffer.rider_id == rider_id, DeliveryOffer.status == "pending"
        )
    )
    return result.scalars().first()


async def get_delivery(session: AsyncSession, delivery_id: str) -> Delivery | None:
    return await session.get(Delivery, delivery_id)


async def get_delivery_by_order(session: AsyncSession, order_id: str) -> Delivery | None:
    result = await session.execute(select(Delivery).where(Delivery.order_id == order_id))
    return result.scalar_one_or_none()


def record_shift_change(
    session: AsyncSession, rider: Rider, from_status: str, *, reason: str
) -> None:
    """Adds a RiderShiftChanged outbox event, in the caller's transaction, if
    the rider went offline ↔ on shift (week3-plan W3-D8c) — the rider's own
    choice, a suspension, or dropping out of a delivery. available ↔ busy is
    not a shift change. Analytics derives time on shift from these."""
    was_on_shift = from_status != RIDER_STATUS_OFFLINE
    on_shift = rider.status != RIDER_STATUS_OFFLINE
    if was_on_shift == on_shift:
        return
    # delivery-worker sends it to Kafka; analytics-service uses it for rider
    # utilization.
    session.add(
        new_outbox_row(
            OutboxEvent,
            aggregate_id=rider.id,
            event_type=EventType.RIDER_SHIFT_CHANGED,
            payload={
                "rider_id": rider.id,
                "rider_user_id": rider.user_id,
                "from_status": from_status,
                "to_status": rider.status,
                "on_shift": on_shift,
                "reason": reason,
            },
        )
    )
