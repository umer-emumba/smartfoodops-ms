"""Rider delivery endpoints and delivery lookup (docs/week2-plan.md step 5).

The rider endpoints check ownership and state for a fast 403/409, then
signal the order saga, which makes the change (app/activities.py) — so a
rider's action and, say, a customer cancellation are applied one at a time,
never racing. They return 202 with the delivery as it is now."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError
from sfo_contracts import OrderSagaSignal
from sfo_contracts.workflows import RiderDropped, RiderResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository, temporal_client
from app.auth_deps import get_current_user, require_roles
from app.db import get_session
from app.models import Delivery, Rider
from app.schemas import DeliveryResponse, DropOutRequest, OfferResponse
from app.state_machine import DeliveryStatus

router = APIRouter(prefix="/v1", tags=["deliveries"])


async def _my_rider(session: AsyncSession, current: CurrentUser) -> Rider:
    rider = await repository.get_rider_by_user_id(session, current.id)
    if rider is None:
        raise NotFoundError("Rider profile not found — register first")
    return rider


async def _my_delivery(session: AsyncSession, current: CurrentUser, delivery_id: str) -> Delivery:
    """The delivery, if it's currently held by the calling rider."""
    rider = await _my_rider(session, current)
    delivery = await repository.get_delivery(session, delivery_id)
    if delivery is None:
        raise NotFoundError("Delivery not found")
    if delivery.rider_id != rider.id:
        raise ForbiddenError("This delivery isn't yours")
    return delivery


async def _signal(
    session: AsyncSession, delivery: Delivery, signal: OrderSagaSignal, *args: object
) -> DeliveryResponse:
    snapshot = DeliveryResponse.model_validate(delivery)
    # Release the DB connection before the network call (expires `delivery`).
    await session.rollback()
    await temporal_client.signal_order_saga(snapshot.order_id, signal, *args)
    return snapshot


@router.get("/riders/me/offer", response_model=OfferResponse)
async def get_my_offer(
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> OfferResponse:
    """The delivery currently offered to the calling rider, if any (404 if
    none). Accept or decline before `expires_at`."""
    rider = await _my_rider(session, current)
    offer = await repository.get_pending_offer(session, rider.id)
    if offer is None:
        raise NotFoundError("No delivery is offered to you right now")
    delivery = await repository.get_delivery(session, offer.delivery_id)
    assert delivery is not None
    return OfferResponse(
        delivery_id=delivery.id,
        order_id=delivery.order_id,
        pickup_lat=delivery.pickup_lat,  # type: ignore[arg-type]
        pickup_lng=delivery.pickup_lng,  # type: ignore[arg-type]
        dropoff_lat=delivery.dropoff_lat,  # type: ignore[arg-type]
        dropoff_lng=delivery.dropoff_lng,  # type: ignore[arg-type]
        delivery_distance_km=delivery.distance_km or 0,
        distance_to_pickup_km=offer.distance_km,
        expires_at=offer.expires_at,
    )


async def _respond(
    session: AsyncSession, current: CurrentUser, delivery_id: str, *, accepted: bool
) -> DeliveryResponse:
    delivery = await _my_delivery(session, current, delivery_id)
    if delivery.status != DeliveryStatus.OFFERED:
        raise ConflictError(f"Delivery is {delivery.status}, not waiting for your answer")
    if delivery.offer_expires_at is not None and delivery.offer_expires_at <= datetime.now(UTC):
        raise ConflictError("This offer has expired")
    return await _signal(
        session,
        delivery,
        OrderSagaSignal.RIDER_RESPONSE,
        RiderResponse(delivery_id=delivery.id, accepted=accepted),
    )


@router.post(
    "/riders/me/deliveries/{delivery_id}/accept", response_model=DeliveryResponse, status_code=202
)
async def accept_delivery(
    delivery_id: str,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    return await _respond(session, current, delivery_id, accepted=True)


@router.post(
    "/riders/me/deliveries/{delivery_id}/decline", response_model=DeliveryResponse, status_code=202
)
async def decline_delivery(
    delivery_id: str,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    return await _respond(session, current, delivery_id, accepted=False)


@router.post(
    "/riders/me/deliveries/{delivery_id}/drop-out",
    response_model=DeliveryResponse,
    status_code=202,
)
async def drop_out(
    delivery_id: str,
    body: DropOutRequest,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    """The rider can't complete an accepted delivery (D12) — another rider is
    found. Before pickup only; after it, the food is with the rider and it's
    a support case."""
    delivery = await _my_delivery(session, current, delivery_id)
    if delivery.status != DeliveryStatus.ASSIGNED:
        raise ConflictError(f"Delivery is {delivery.status}; drop-out is only before pickup")
    return await _signal(
        session,
        delivery,
        OrderSagaSignal.RIDER_DROPPED,
        RiderDropped(delivery_id=delivery.id, reason=body.reason),
    )


@router.post(
    "/riders/me/deliveries/{delivery_id}/picked-up",
    response_model=DeliveryResponse,
    status_code=202,
)
async def mark_picked_up(
    delivery_id: str,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    delivery = await _my_delivery(session, current, delivery_id)
    if delivery.status != DeliveryStatus.ASSIGNED:
        raise ConflictError(f"Delivery is {delivery.status}, not assigned")
    return await _signal(session, delivery, OrderSagaSignal.PICKED_UP)


@router.post(
    "/riders/me/deliveries/{delivery_id}/delivered",
    response_model=DeliveryResponse,
    status_code=202,
)
async def mark_delivered(
    delivery_id: str,
    current: CurrentUser = Depends(require_roles(Role.RIDER)),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    delivery = await _my_delivery(session, current, delivery_id)
    if delivery.status != DeliveryStatus.PICKED_UP:
        raise ConflictError(f"Delivery is {delivery.status}, not picked up")
    return await _signal(session, delivery, OrderSagaSignal.DELIVERED)


@router.get("/deliveries/by-order/{order_id}", response_model=DeliveryResponse)
async def get_delivery_for_order(
    order_id: str,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Delivery:
    """Delivery status, rider and ETAs for an order — the ordering customer,
    the rider holding it, or a system_admin."""
    delivery = await repository.get_delivery_by_order(session, order_id)
    if delivery is None:
        raise NotFoundError("No delivery for this order (yet)")
    if current.role == Role.SYSTEM_ADMIN or current.id == delivery.customer_id:
        return delivery
    if current.role == Role.RIDER and delivery.rider_id is not None:
        rider = await repository.get_rider_by_user_id(session, current.id)
        if rider is not None and rider.id == delivery.rider_id:
            return delivery
    raise ForbiddenError("Not your delivery")
