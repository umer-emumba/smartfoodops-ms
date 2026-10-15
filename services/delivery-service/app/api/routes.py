import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from redis.exceptions import RedisError
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError
from sfo_common.pagination import Page
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository, rider_locations, tracking
from app.auth_deps import require_roles
from app.config import settings
from app.db import get_session
from app.models import Rider, RiderVerificationHistory
from app.mongo import insert_location_ping
from app.schemas import (
    RiderLocationUpdate,
    RiderRegister,
    RiderResponse,
    RiderStatusUpdate,
    StatusChangeRequest,
    VerificationHistoryResponse,
)

router = APIRouter(prefix="/v1", tags=["riders"])
_logger = logging.getLogger(__name__)

# Rider registration, vetting, status and location. Delivery offers and the
# rider's delivery actions: app/api/deliveries.py.


async def _sync_pool(rider: Rider) -> None:
    """Keeps the Redis assignment pool (app/rider_locations.py) in line with
    the rider's status. Best-effort: assignment re-checks Postgres anyway."""
    try:
        if rider.status == "available" and rider.verification_status == "approved":
            fallback = (rider.last_lat, rider.last_lng) if rider.last_lat is not None else None
            await rider_locations.make_available(rider.id, fallback)  # type: ignore[arg-type]
        else:
            await rider_locations.remove(rider.id)
    except RedisError:
        _logger.warning("Rider pool update failed", exc_info=True)


def _record_verification_change(
    session: AsyncSession,
    rider: Rider,
    to_status: str,
    current: CurrentUser,
    reason: str | None = None,
) -> None:
    """Changes verification_status and appends the audit row in the same
    transaction. A no-op transition records nothing."""
    from_status = rider.verification_status
    if from_status == to_status:
        return
    rider.verification_status = to_status
    session.add(
        RiderVerificationHistory(
            rider_id=rider.id,
            from_status=from_status,
            to_status=to_status,
            actor_id=current.id,
            actor_role=current.role.value,
            reason=reason,
        )
    )


@router.post("/riders", response_model=RiderResponse, status_code=201)
async def register_rider(
    body: RiderRegister,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    if await repository.get_rider_by_user_id(session, current.id) is not None:
        raise ConflictError("Rider profile already exists")
    rider = Rider(user_id=current.id, verification_status="pending_approval", **body.model_dump())
    try:
        rider = await repository.create_rider(session, rider)
        session.add(
            RiderVerificationHistory(
                rider_id=rider.id,
                from_status=None,
                to_status=rider.verification_status,
                actor_id=current.id,
                actor_role=current.role.value,
            )
        )
        await session.commit()
    except IntegrityError as exc:
        # Concurrent double-submit: both passed the pre-check above.
        await session.rollback()
        raise ConflictError("Rider profile already exists") from exc
    return rider


@router.get("/riders/me", response_model=RiderResponse)
async def get_my_rider_profile(
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    rider = await repository.get_rider_by_user_id(session, current.id)
    if rider is None:
        raise NotFoundError("Rider profile not found — register first")
    return rider


@router.patch("/riders/me/status", response_model=RiderResponse)
async def update_my_status(
    body: RiderStatusUpdate,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    rider = await repository.get_rider_by_user_id(session, current.id)
    if rider is None:
        raise NotFoundError("Rider profile not found — register first")
    # Only vetted riders may go on shift — keeps pending/suspended riders out
    # of Week 2's assignment pool at the source. Going offline is always allowed.
    if body.status != "offline" and rider.verification_status != "approved":
        raise ForbiddenError("Rider is not approved to go on shift")
    # While holding an offer or a delivery the rider is `busy` on the
    # saga's behalf; going offline mid-delivery is a drop-out instead.
    if body.status != rider.status and (
        await repository.get_active_delivery_for_rider(session, rider.id)
    ):
        raise ConflictError("You have an active delivery — finish it or drop out first")
    from_status = rider.status
    rider.status = body.status
    repository.record_shift_change(session, rider, from_status, reason="rider")
    await session.commit()
    await _sync_pool(rider)
    return rider


@router.patch("/riders/me/location", response_model=RiderResponse)
async def update_my_location(
    body: RiderLocationUpdate,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    """Plain REST POST/PATCH, not a persistent connection — see
    docs/architecture.md#real-time-delivery-tracking for why. Sent every
    5–15s while on shift.

    The live position goes to Redis (week2-plan D6) — the assignment pool if
    the rider is available, and the customer's live tracking if the rider has
    their order. Postgres keeps it at most every 30s (the stored
    `last_lat`/`last_lng` in the response may lag by that much)."""
    rider = await repository.get_rider_by_user_id(session, current.id)
    if rider is None:
        raise NotFoundError("Rider profile not found — register first")
    now = datetime.now(UTC)
    try:
        await rider_locations.record(
            rider.id,
            body.lat,
            body.lng,
            available=rider.status == "available" and rider.verification_status == "approved",
        )
    except RedisError:
        _logger.warning("Rider live location update failed", exc_info=True)
    db_interval = timedelta(seconds=settings.rider_location_db_interval_seconds)
    if rider.last_location_at is None or now - rider.last_location_at >= db_interval:
        rider.last_lat, rider.last_lng, rider.last_location_at = body.lat, body.lng, now
    active = await repository.get_active_delivery_for_rider(session, rider.id)
    tracked_order = (
        active.order_id
        if active is not None and active.status in ("assigned", "picked_up")
        else None
    )
    await session.commit()
    if tracked_order is not None:
        await tracking.publish_location(tracked_order, rider.id, body.lat, body.lng)
    # Full ping history goes to Mongo, not delivery_db — Postgres only ever
    # needs the latest position (docs/architecture.md#services). Best-effort:
    # Mongo is non-authoritative, so a Mongo outage loses one history point
    # instead of failing a location update that already committed.
    try:
        await insert_location_ping(rider.id, body.lat, body.lng)
    except Exception:
        _logger.warning("Failed to record rider location ping", exc_info=True)
    return rider


@router.get("/riders/admin", response_model=Page[RiderResponse])
async def list_riders_admin(
    verification_status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await repository.list_riders(
        session, verification_status=verification_status, cursor=cursor, limit=limit
    )


@router.post("/riders/{rider_id}/approve", response_model=RiderResponse)
async def approve_rider(
    rider_id: str,
    body: StatusChangeRequest | None = None,
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    rider = await repository.get_rider(session, rider_id)
    if rider is None:
        raise NotFoundError("Rider not found")
    _record_verification_change(session, rider, "approved", current, body.reason if body else None)
    await session.commit()
    return rider


@router.post("/riders/{rider_id}/suspend", response_model=RiderResponse)
async def suspend_rider(
    rider_id: str,
    body: StatusChangeRequest | None = None,
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    """Covers both rejecting a pending rider and suspending an already-approved
    one — see suspend_restaurant's docstring for the same reasoning."""
    rider = await repository.get_rider(session, rider_id)
    if rider is None:
        raise NotFoundError("Rider not found")
    _record_verification_change(session, rider, "suspended", current, body.reason if body else None)
    # A suspended rider is taken off shift immediately, not left "available".
    from_status = rider.status
    rider.status = "offline"
    repository.record_shift_change(session, rider, from_status, reason="suspended")
    await session.commit()
    await _sync_pool(rider)
    return rider


@router.get(
    "/riders/{rider_id}/verification-history", response_model=list[VerificationHistoryResponse]
)
async def get_verification_history(
    rider_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[RiderVerificationHistory]:
    if await repository.get_rider(session, rider_id) is None:
        raise NotFoundError("Rider not found")
    return await repository.list_verification_history(session, rider_id)
