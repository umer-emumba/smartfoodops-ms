"""delivery-service's activities on `delivery-activities` (docs/week2-plan.md
step 5): deliveries, rider offers and ETAs for the order saga.

Assignment (D6): the nearest available riders come from Redis GEO; a rider
is claimed with one atomic `UPDATE riders SET status='busy' WHERE ...
status='available'`, so two deliveries can never get the same rider.

Every status change, creation included, writes a history row and a
`DeliveryStatusUpdated` outbox event in the same transaction; new ETAs write
a `DeliveryEtaUpdated` event. Those events are also what the live feeds show
(tracking-service consumes them): the customer's delivery status and ETAs,
and the rider's offers. Each activity locks the delivery row and is safe to
re-run (Temporal retries)."""

from datetime import UTC, datetime, timedelta

from sfo_common.geo import haversine_km
from sfo_contracts import BusinessErrorType, business_error
from sfo_contracts.activities import (
    CreateDeliveryInput,
    CreateDeliveryResult,
    DeliveryActivity,
    DeliveryEta,
    DeliveryRef,
    OfferRiderInput,
    OrderRef,
    ReleaseRiderInput,
    RiderOffer,
    RiderReleaseOutcome,
    TransitionDeliveryInput,
)
from sfo_events import EventType, new_outbox_row
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app import eta, repository, rider_locations
from app.config import settings
from app.db import SessionFactory
from app.models import Delivery, DeliveryOffer, DeliveryStatusHistory, OutboxEvent, Rider
from app.state_machine import DeliveryStatus, can_transition

D = DeliveryStatus

# Offer outcomes that put the delivery back up for assignment.
_REASSIGN = {
    RiderReleaseOutcome.DECLINED,
    RiderReleaseOutcome.EXPIRED,
    RiderReleaseOutcome.DROPPED,
    RiderReleaseOutcome.LATE,
}
_OPEN_OFFER = ("pending", "accepted")
_BEFORE_PICKUP = (D.PENDING_ASSIGNMENT, D.OFFERED, D.ASSIGNED)


def _now() -> datetime:
    return datetime.now(UTC)


async def _locked(session: AsyncSession, delivery_id: str) -> Delivery:
    result = await session.execute(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise business_error(BusinessErrorType.NOT_FOUND, f"Delivery {delivery_id} not found")
    return delivery


def _invalid(delivery: Delivery, action: str) -> Exception:
    return business_error(
        BusinessErrorType.INVALID_DELIVERY_STATE,
        f"Can't {action} delivery {delivery.id}: it is {delivery.status}",
    )


async def _user_id_of(session: AsyncSession, rider_id: str | None) -> str | None:
    if rider_id is None:
        return None
    return await session.scalar(select(Rider.user_id).where(Rider.id == rider_id))


async def _status_event(
    session: AsyncSession,
    delivery: Delivery,
    from_status: str | None,
    *,
    reason: str | None = None,
    released_rider_user_id: str | None = None,
) -> OutboxEvent:
    """The `DeliveryStatusUpdated` outbox row for the delivery's current
    status. It names the rider's user account so notification-service and
    the rider's live feed can reach them without asking this service
    (week3-plan W3-D8)."""
    return new_outbox_row(
        OutboxEvent,
        aggregate_id=delivery.id,
        event_type=EventType.DELIVERY_STATUS_UPDATED,
        payload={
            "delivery_id": delivery.id,
            "order_id": delivery.order_id,
            "customer_id": delivery.customer_id,
            "rider_id": delivery.rider_id,
            "rider_user_id": await _user_id_of(session, delivery.rider_id),
            "from_status": from_status,
            "to_status": delivery.status,
            "reason": reason,
            "released_rider_user_id": released_rider_user_id,
            "pickup_eta": delivery.pickup_eta,
            "dropoff_eta": delivery.dropoff_eta,
        },
    )


async def _set_status(
    session: AsyncSession,
    delivery: Delivery,
    to_status: DeliveryStatus,
    reason: str | None = None,
    *,
    released_rider_user_id: str | None = None,
) -> None:
    """Status + history row + outbox event, in the caller's transaction.
    delivery-worker sends the event to Kafka: notification-service tells the
    rider (new offer, assigned, cancelled), analytics-service tracks the
    delivery, tracking-service updates the live feeds."""
    from_status = delivery.status
    delivery.status = to_status
    session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id,
            from_status=from_status,
            to_status=to_status,
            rider_id=delivery.rider_id,
            reason=reason,
        )
    )
    session.add(
        await _status_event(
            session,
            delivery,
            from_status,
            reason=reason,
            released_rider_user_id=released_rider_user_id,
        )
    )


async def _open_offer(session: AsyncSession, delivery: Delivery) -> DeliveryOffer | None:
    if delivery.rider_id is None:
        return None
    result = await session.execute(
        select(DeliveryOffer).where(
            DeliveryOffer.delivery_id == delivery.id,
            DeliveryOffer.rider_id == delivery.rider_id,
            DeliveryOffer.status.in_(_OPEN_OFFER),
        )
    )
    return result.scalar_one_or_none()


async def _free_rider(session: AsyncSession, rider_id: str, *, to_status: str) -> Rider | None:
    """Releases a rider held by this service (`busy`) — back on shift, or
    off it after dropping out. Returns the rider if it was released."""
    result = await session.execute(
        update(Rider)
        .where(Rider.id == rider_id, Rider.status == "busy")
        .values(status=to_status)
        .returning(Rider)
    )
    return result.scalar_one_or_none()


async def _after_release(rider: Rider | None) -> None:
    if rider is not None and rider.status == "available":
        fallback = (rider.last_lat, rider.last_lng) if rider.last_lat is not None else None
        await rider_locations.make_available(rider.id, fallback)  # type: ignore[arg-type]


@activity.defn(name=DeliveryActivity.CREATE_DELIVERY)
async def create_delivery(data: CreateDeliveryInput) -> CreateDeliveryResult:
    """One delivery per order: an existing one (including a cancelled
    placeholder, see cancel_delivery) is returned as-is."""
    async with SessionFactory() as session:
        existing = (
            await session.execute(select(Delivery).where(Delivery.order_id == data.order_id))
        ).scalar_one_or_none()
        if existing is not None:
            return CreateDeliveryResult(delivery_id=existing.id, status=existing.status)
        delivery = Delivery(
            order_id=data.order_id,
            customer_id=data.customer_id,
            status=D.PENDING_ASSIGNMENT,
            pickup_lat=data.pickup.lat,
            pickup_lng=data.pickup.lng,
            dropoff_lat=data.dropoff.lat,
            dropoff_lng=data.dropoff.lng,
            distance_km=round(
                haversine_km(data.pickup.lat, data.pickup.lng, data.dropoff.lat, data.dropoff.lng),
                2,
            ),
            ready_at=data.ready_at,
        )
        session.add(delivery)
        try:
            await session.flush()
            session.add(
                DeliveryStatusHistory(
                    delivery_id=delivery.id, from_status=None, to_status=D.PENDING_ASSIGNMENT
                )
            )
            # The customer's live feed shows "looking for a rider" from this.
            session.add(await _status_event(session, delivery, None))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await create_delivery(data)
        return CreateDeliveryResult(delivery_id=delivery.id, status=delivery.status)


@activity.defn(name=DeliveryActivity.OFFER_TO_NEAREST_RIDER)
async def offer_to_nearest_rider(data: OfferRiderInput) -> RiderOffer | None:
    """Offers the delivery to the nearest available, approved rider within
    the saga's search radius (excluding riders who had it recently), holding that
    rider (`busy`) until they answer or the offer expires. None if nobody is
    free. Re-running while the offer is open returns the same offer."""
    async with SessionFactory() as session:
        delivery = await _locked(session, data.delivery_id)
        if delivery.status == D.OFFERED:
            offer = await _open_offer(session, delivery)
            assert offer is not None
            return RiderOffer(
                rider_id=offer.rider_id, distance_km=offer.distance_km, expires_at=offer.expires_at
            )
        if delivery.status != D.PENDING_ASSIGNMENT:
            raise _invalid(delivery, "offer")
        assert delivery.pickup_lat is not None and delivery.pickup_lng is not None

        candidates = await rider_locations.nearest_available(
            delivery.pickup_lat,
            delivery.pickup_lng,
            data.radius_km or settings.rider_search_radius_km,
        )
        excluded = set(data.exclude_rider_ids)
        claimed: tuple[str, float] | None = None
        unavailable: list[str] = []
        for rider_id, km in candidates:
            if rider_id in excluded:
                continue
            # Row-locked until commit: a concurrent offer trying the same
            # rider waits here, then finds them busy and moves on.
            result = await session.execute(
                update(Rider)
                .where(
                    Rider.id == rider_id,
                    Rider.status == "available",
                    Rider.verification_status == "approved",
                )
                .values(status="busy")
                .returning(Rider.id)
            )
            if result.first() is not None:
                claimed = (rider_id, km)
                break
            unavailable.append(rider_id)
        if claimed is None:
            await session.rollback()
            for rider_id in unavailable:
                await rider_locations.remove(rider_id)
            return None

        rider_id, km = claimed
        timeout = data.offer_timeout_seconds or settings.rider_offer_timeout_seconds
        expires_at = _now() + timedelta(seconds=timeout)
        session.add(
            DeliveryOffer(
                delivery_id=delivery.id,
                rider_id=rider_id,
                distance_km=round(km, 2),
                expires_at=expires_at,
            )
        )
        delivery.rider_id = rider_id
        delivery.offer_expires_at = expires_at
        await _set_status(session, delivery, D.OFFERED)
        await session.commit()
    # Out of the pool now; riders whose claim failed were stale entries.
    for stale_or_claimed in [rider_id, *unavailable]:
        await rider_locations.remove(stale_or_claimed)
    return RiderOffer(rider_id=rider_id, distance_km=round(km, 2), expires_at=expires_at)


@activity.defn(name=DeliveryActivity.RELEASE_RIDER)
async def release_rider(data: ReleaseRiderInput) -> None:
    """Ends the rider's hold on the delivery with `outcome`:

    - declined / expired / dropped: the delivery goes back to
      `pending_assignment` (reassignment_count + 1); a dropped-out rider goes
      off shift, the others back into the pool;
    - completed (after delivery): the rider is free again.

    No-op if the rider's offer is already closed (re-run)."""
    async with SessionFactory() as session:
        delivery = await _locked(session, data.delivery_id)
        offer = await _open_offer(session, delivery)
        if offer is None:
            return
        offer.status = data.outcome.value
        offer.responded_at = offer.responded_at or _now()
        rider = await _free_rider(
            session,
            offer.rider_id,
            to_status="offline" if data.outcome == RiderReleaseOutcome.DROPPED else "available",
        )
        if rider is not None:
            repository.record_shift_change(session, rider, "busy", reason="dropped_out")
        if data.outcome in _REASSIGN:
            if delivery.status not in (D.OFFERED, D.ASSIGNED):
                raise _invalid(delivery, f"release ({data.outcome})")
            # Before clearing it: the event tells this rider they lost it.
            released = await _user_id_of(session, delivery.rider_id)
            delivery.rider_id = None
            delivery.offer_expires_at = None
            delivery.pickup_eta = delivery.dropoff_eta = None
            delivery.reassignment_count += 1
            await _set_status(
                session,
                delivery,
                D.PENDING_ASSIGNMENT,
                reason=f"rider_{data.outcome}",
                released_rider_user_id=released,
            )
        await session.commit()
    await _after_release(rider)


@activity.defn(name=DeliveryActivity.TRANSITION_DELIVERY)
async def transition_delivery(data: TransitionDeliveryInput) -> None:
    """The saga moving the delivery forward after a rider's signal:
    offered → assigned (accepted), → picked_up, → delivered. Already there →
    no-op."""
    to_status = DeliveryStatus(data.to_status)
    async with SessionFactory() as session:
        delivery = await _locked(session, data.delivery_id)
        if delivery.status == to_status:
            return
        if not can_transition(delivery.status, to_status):
            raise _invalid(delivery, f"move to {to_status}")
        now = _now()
        if to_status == D.ASSIGNED:
            offer = await _open_offer(session, delivery)
            assert offer is not None
            offer.status = "accepted"
            offer.responded_at = now
            delivery.assigned_at = now
            delivery.offer_expires_at = None
        elif to_status == D.PICKED_UP:
            delivery.picked_up_at = now
        elif to_status == D.DELIVERED:
            delivery.delivered_at = now
        await _set_status(session, delivery, to_status)
        await session.commit()


async def _estimate(session: AsyncSession, delivery: Delivery) -> tuple[datetime, datetime]:
    """Pickup and drop-off ETAs for the rider holding `delivery` (app/eta.py)."""
    rider = await session.get(Rider, delivery.rider_id)
    assert rider is not None
    position = await rider_locations.latest(rider.id)
    if position is None and rider.last_lat is not None and rider.last_lng is not None:
        position = (rider.last_lat, rider.last_lng)
    assert delivery.pickup_lat is not None and delivery.pickup_lng is not None
    assert delivery.dropoff_lat is not None and delivery.dropoff_lng is not None
    return eta.estimate(
        now=_now(),
        rider_position=position,
        pickup=(delivery.pickup_lat, delivery.pickup_lng),
        dropoff=(delivery.dropoff_lat, delivery.dropoff_lng),
        vehicle_type=rider.vehicle_type,
        ready_at=delivery.ready_at,
    )


@activity.defn(name=DeliveryActivity.COMPUTE_ETA)
async def compute_eta(data: DeliveryRef) -> DeliveryEta:
    """Pickup and drop-off ETAs for the assigned rider (app/eta.py), stored
    on the delivery with a `DeliveryEtaUpdated` event (the customer's live
    feed shows them from it)."""
    async with SessionFactory() as session:
        delivery = await _locked(session, data.delivery_id)
        if delivery.rider_id is None:
            raise _invalid(delivery, "estimate ETA for")
        pickup_eta, dropoff_eta = await _estimate(session, delivery)
        delivery.pickup_eta, delivery.dropoff_eta = pickup_eta, dropoff_eta
        session.add(
            new_outbox_row(
                OutboxEvent,
                aggregate_id=delivery.id,
                event_type=EventType.DELIVERY_ETA_UPDATED,
                payload={
                    "delivery_id": delivery.id,
                    "order_id": delivery.order_id,
                    "customer_id": delivery.customer_id,
                    "rider_id": delivery.rider_id,
                    "status": delivery.status,
                    "pickup_eta": pickup_eta,
                    "dropoff_eta": dropoff_eta,
                },
            )
        )
        await session.commit()
    return DeliveryEta(pickup_eta=pickup_eta, dropoff_eta=dropoff_eta)


@activity.defn(name=DeliveryActivity.MARK_FOOD_READY)
async def mark_food_ready(data: DeliveryRef) -> DeliveryEta | None:
    """The restaurant marked the food ready: `food_ready_at` is set and
    `ready_at` becomes the real time, the assigned rider's ETAs are
    recomputed from it (and returned), and a `DeliveryFoodReady` event tells
    the customer and that rider. No-op (None) if already marked, or once
    picked up or cancelled (a cancel may compensate while this runs); None
    too while no rider is assigned."""
    async with SessionFactory() as session:
        delivery = await _locked(session, data.delivery_id)
        if delivery.food_ready_at is not None or delivery.status not in _BEFORE_PICKUP:
            return None
        delivery.food_ready_at = delivery.ready_at = _now()
        rider_user_id, new_eta = None, None
        if delivery.status == D.ASSIGNED:
            pickup_eta, dropoff_eta = await _estimate(session, delivery)
            delivery.pickup_eta, delivery.dropoff_eta = pickup_eta, dropoff_eta
            new_eta = DeliveryEta(pickup_eta=pickup_eta, dropoff_eta=dropoff_eta)
            rider_user_id = await _user_id_of(session, delivery.rider_id)
        session.add(
            new_outbox_row(
                OutboxEvent,
                aggregate_id=delivery.id,
                event_type=EventType.DELIVERY_FOOD_READY,
                payload={
                    "delivery_id": delivery.id,
                    "order_id": delivery.order_id,
                    "customer_id": delivery.customer_id,
                    "rider_id": delivery.rider_id,
                    "rider_user_id": rider_user_id,
                    "status": delivery.status,
                    "food_ready_at": delivery.food_ready_at,
                    "pickup_eta": delivery.pickup_eta,
                    "dropoff_eta": delivery.dropoff_eta,
                },
            )
        )
        await session.commit()
    return new_eta


@activity.defn(name=DeliveryActivity.CANCEL_DELIVERY)
async def cancel_delivery(data: OrderRef) -> None:
    """Compensation for create_delivery (by order, like the saga's other
    compensations): releases any rider and cancels the delivery. Before
    pickup only. No delivery yet → a `cancelled` placeholder, so a late
    create_delivery doesn't start looking for riders."""
    async with SessionFactory() as session:
        result = await session.execute(
            select(Delivery).where(Delivery.order_id == data.order_id).with_for_update()
        )
        delivery = result.scalar_one_or_none()
        if delivery is None:
            session.add(Delivery(order_id=data.order_id, status=D.CANCELLED, cancelled_at=_now()))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await cancel_delivery(data)
            return
        if delivery.status == D.CANCELLED:
            return
        if not can_transition(delivery.status, D.CANCELLED):
            raise _invalid(delivery, "cancel")
        rider = None
        offer = await _open_offer(session, delivery)
        if offer is not None:
            offer.status = RiderReleaseOutcome.CANCELLED.value
            offer.responded_at = offer.responded_at or _now()
            rider = await _free_rider(session, offer.rider_id, to_status="available")
        delivery.cancelled_at = _now()
        await _set_status(session, delivery, D.CANCELLED, reason="order_cancelled")
        await session.commit()
    await _after_release(rider)


ACTIVITIES = [
    create_delivery,
    offer_to_nearest_rider,
    release_rider,
    transition_delivery,
    compute_eta,
    mark_food_ready,
    cancel_delivery,
]
