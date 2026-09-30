from fastapi import APIRouter, Depends, Query
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import require_roles
from app.db import get_session
from app.models import Rider
from app.mongo import insert_location_ping
from app.schemas import RiderLocationUpdate, RiderRegister, RiderResponse, RiderStatusUpdate

router = APIRouter(prefix="/v1", tags=["riders"])

# Week 1: rider registration + status + location only. Rider assignment, ETA,
# and the customer-facing tracking flow land in Week 2 — see
# docs/architecture.md#real-time-delivery-tracking.


@router.post("/riders", response_model=RiderResponse, status_code=201)
async def register_rider(
    body: RiderRegister,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    if await repository.get_rider_by_user_id(session, current.id) is not None:
        raise ConflictError("Rider profile already exists")
    rider = Rider(user_id=current.id, **body.model_dump())
    rider = await repository.create_rider(session, rider)
    await session.commit()
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
    rider.status = body.status
    await session.commit()
    return rider


@router.patch("/riders/me/location", response_model=RiderResponse)
async def update_my_location(
    body: RiderLocationUpdate,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    """Plain REST POST/PATCH, not a persistent connection — see
    docs/architecture.md#real-time-delivery-tracking for why."""
    rider = await repository.get_rider_by_user_id(session, current.id)
    if rider is None:
        raise NotFoundError("Rider profile not found — register first")
    rider.last_lat = body.lat
    rider.last_lng = body.lng
    await session.commit()
    # Full ping history goes to Mongo, not delivery_db — Postgres only ever
    # needs the latest position (docs/architecture.md#services).
    await insert_location_ping(rider.id, body.lat, body.lng)
    return rider


@router.get("/riders/admin", response_model=list[RiderResponse])
async def list_riders_admin(
    verification_status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[Rider]:
    return await repository.list_riders(session, verification_status=verification_status, limit=limit)


@router.post("/riders/{rider_id}/approve", response_model=RiderResponse)
async def approve_rider(
    rider_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    rider = await repository.get_rider(session, rider_id)
    if rider is None:
        raise NotFoundError("Rider not found")
    rider.verification_status = "approved"
    await session.commit()
    return rider


@router.post("/riders/{rider_id}/suspend", response_model=RiderResponse)
async def suspend_rider(
    rider_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Rider:
    """Covers both rejecting a pending rider and suspending an already-approved
    one — see suspend_restaurant's docstring for the same reasoning."""
    rider = await repository.get_rider(session, rider_id)
    if rider is None:
        raise NotFoundError("Rider not found")
    rider.verification_status = "suspended"
    await session.commit()
    return rider
