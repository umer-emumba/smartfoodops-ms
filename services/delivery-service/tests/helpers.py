"""Riders and deliveries for the `db` tests, set up through the real
activities where possible."""

from datetime import UTC, datetime
from typing import Any

from app import activities, rider_locations
from app.models import OutboxEvent, Rider
from sfo_contracts.activities import (
    CreateDeliveryInput,
    GeoPoint,
    OfferRiderInput,
    RiderOffer,
    TransitionDeliveryInput,
)
from sqlalchemy import select

from tests.conftest import Env

# Restaurant in Clifton, Karachi; customer ~2.2 km north.
PICKUP = GeoPoint(lat=24.8138, lng=67.0300)
DROPOFF = GeoPoint(lat=24.8338, lng=67.0300)
CUSTOMER = "u-customer"


def km_north(km: float) -> tuple[float, float]:
    """A point `km` north of the restaurant (1° latitude ≈ 111.2 km)."""
    return PICKUP.lat + km / 111.2, PICKUP.lng


async def add_rider(
    env: Env,
    rider_id: str,
    *,
    at: tuple[float, float] | None = None,
    status: str = "available",
    verification_status: str = "approved",
    vehicle_type: str = "bike",
) -> None:
    async with env.factory() as session:
        session.add(
            Rider(
                id=rider_id,
                user_id=f"user-{rider_id}",
                status=status,
                verification_status=verification_status,
                vehicle_type=vehicle_type,
                last_lat=at[0] if at else None,
                last_lng=at[1] if at else None,
                last_location_at=datetime.now(UTC) if at else None,
            )
        )
        await session.commit()
    if at is not None:
        await rider_locations.record(rider_id, *at, available=status == "available")


async def new_delivery(env: Env, order_id: str = "o-1") -> str:
    result = await activities.create_delivery(
        CreateDeliveryInput(order_id=order_id, customer_id=CUSTOMER, pickup=PICKUP, dropoff=DROPOFF)
    )
    return result.delivery_id


async def offered(env: Env, order_id: str = "o-1") -> tuple[str, RiderOffer]:
    delivery_id = await new_delivery(env, order_id)
    offer = await activities.offer_to_nearest_rider(OfferRiderInput(delivery_id=delivery_id))
    assert offer is not None
    return delivery_id, offer


async def assigned(env: Env, order_id: str = "o-1") -> tuple[str, RiderOffer]:
    delivery_id, offer = await offered(env, order_id)
    await activities.transition_delivery(
        TransitionDeliveryInput(delivery_id=delivery_id, to_status="assigned")
    )
    return delivery_id, offer


async def outbox_payloads(env: Env, event_type: str) -> list[dict[str, Any]]:
    """Payloads of `event_type` outbox rows, oldest first."""
    async with env.factory() as session:
        rows = await session.execute(
            select(OutboxEvent.payload)
            .where(OutboxEvent.event_type == event_type)
            .order_by(OutboxEvent.created_at)
        )
        return list(rows.scalars().all())
