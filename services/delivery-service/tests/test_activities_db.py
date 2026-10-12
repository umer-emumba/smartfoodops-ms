"""delivery-activities against a real Postgres and Redis (week2-plan step 5)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app import activities, rider_locations
from app.models import Delivery, DeliveryOffer, DeliveryStatusHistory, OutboxEvent, Rider
from sfo_contracts import BusinessErrorType
from sfo_contracts.activities import (
    CreateDeliveryInput,
    DeliveryRef,
    OfferRiderInput,
    OrderRef,
    ReleaseRiderInput,
    RiderReleaseOutcome,
    TransitionDeliveryInput,
)
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from tests.conftest import Env
from tests.helpers import (
    CUSTOMER,
    DROPOFF,
    PICKUP,
    add_rider,
    assigned,
    km_north,
    new_delivery,
    offered,
)

pytestmark = pytest.mark.db


async def _delivery(env: Env, delivery_id: str) -> Delivery:
    async with env.factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        assert delivery is not None
        return delivery


async def _rider(env: Env, rider_id: str) -> Rider:
    async with env.factory() as session:
        rider = await session.get(Rider, rider_id)
        assert rider is not None
        return rider


async def _in_pool(env: Env, rider_id: str) -> bool:
    return await env.redis.zscore(rider_locations.AVAILABLE_KEY, rider_id) is not None


async def _offer(delivery_id: str, exclude: list[str] | None = None):  # type: ignore[no-untyped-def]
    return await activities.offer_to_nearest_rider(
        OfferRiderInput(delivery_id=delivery_id, exclude_rider_ids=exclude or [])
    )


def _release(delivery_id: str, outcome: RiderReleaseOutcome) -> ReleaseRiderInput:
    return ReleaseRiderInput(delivery_id=delivery_id, outcome=outcome)


# ---------- Offers: nearest rider, radius, exclusions ----------


async def test_offers_to_the_nearest_of_three_riders(env: Env) -> None:
    await add_rider(env, "far", at=km_north(4))
    await add_rider(env, "near", at=km_north(0.5))
    await add_rider(env, "mid", at=km_north(2))
    delivery_id = await new_delivery(env)

    offer = await _offer(delivery_id)
    assert offer is not None
    assert offer.rider_id == "near"
    assert offer.distance_km == pytest.approx(0.5, abs=0.01)

    delivery = await _delivery(env, delivery_id)
    assert (delivery.status, delivery.rider_id) == ("offered", "near")
    assert delivery.offer_expires_at == offer.expires_at
    assert (await _rider(env, "near")).status == "busy"
    assert not await _in_pool(env, "near")
    assert env.published[-1][1]["status"] == "offered"


async def test_riders_beyond_the_search_radius_are_not_offered(env: Env) -> None:
    await add_rider(env, "too-far", at=km_north(6))
    delivery_id = await new_delivery(env)
    assert await _offer(delivery_id) is None
    assert (await _delivery(env, delivery_id)).status == "pending_assignment"
    assert (await _rider(env, "too-far")).status == "available"


async def test_excluded_riders_are_skipped(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    await add_rider(env, "mid", at=km_north(2))
    delivery_id = await new_delivery(env)
    offer = await _offer(delivery_id, exclude=["near"])
    assert offer is not None and offer.rider_id == "mid"


async def test_stale_pool_entries_are_skipped_and_cleaned_up(env: Env) -> None:
    # In the Redis pool, but offline / not approved in Postgres.
    await add_rider(env, "went-offline", at=km_north(0.2))
    await add_rider(env, "unvetted", at=km_north(0.4), verification_status="suspended")
    async with env.factory() as session:
        rider = await session.get(Rider, "went-offline")
        assert rider is not None
        rider.status = "offline"
        await session.commit()
    await add_rider(env, "ok", at=km_north(1))

    offer = await _offer(await new_delivery(env))
    assert offer is not None and offer.rider_id == "ok"
    assert not await _in_pool(env, "went-offline")
    assert not await _in_pool(env, "unvetted")


async def test_offer_is_idempotent_while_open(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    await add_rider(env, "mid", at=km_north(2))
    delivery_id, first = await offered(env)
    again = await _offer(delivery_id)  # retried activity
    assert again == first
    async with env.factory() as session:
        count = await session.scalar(select(func.count()).select_from(DeliveryOffer))
    assert count == 1


# ---------- Concurrency: a rider is never claimed twice ----------


async def test_two_deliveries_cannot_claim_the_same_rider(env: Env) -> None:
    await add_rider(env, "only", at=km_north(0.5))
    a, b = await new_delivery(env, "o-a"), await new_delivery(env, "o-b")
    offers = await asyncio.gather(_offer(a), _offer(b))
    assert sorted(o.rider_id if o else "none" for o in offers) == ["none", "only"]


async def test_concurrent_deliveries_get_different_riders(env: Env) -> None:
    for n in range(5):
        await add_rider(env, f"r{n}", at=km_north(0.3 + n * 0.2))
    deliveries = [await new_delivery(env, f"o-{n}") for n in range(8)]
    offers = await asyncio.gather(*(_offer(d) for d in deliveries))
    riders = [o.rider_id for o in offers if o is not None]
    assert len(riders) == 5 and len(set(riders)) == 5  # 5 riders, 8 deliveries
    assert offers.count(None) == 3


# ---------- Release: decline / expire / drop out / complete ----------


@pytest.mark.parametrize("outcome", [RiderReleaseOutcome.DECLINED, RiderReleaseOutcome.EXPIRED])
async def test_declined_or_expired_offer_puts_delivery_back(
    env: Env, outcome: RiderReleaseOutcome
) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await offered(env)
    await activities.release_rider(_release(delivery_id, outcome))
    await activities.release_rider(_release(delivery_id, outcome))  # retry: no-op

    delivery = await _delivery(env, delivery_id)
    assert (delivery.status, delivery.rider_id, delivery.reassignment_count) == (
        "pending_assignment",
        None,
        1,
    )
    assert (await _rider(env, "near")).status == "available"
    assert await _in_pool(env, "near")
    async with env.factory() as session:
        offer = (await session.execute(select(DeliveryOffer))).scalar_one()
    assert offer.status == outcome.value


async def test_dropped_rider_goes_off_shift_and_delivery_is_reoffered(env: Env) -> None:
    await add_rider(env, "first", at=km_north(0.5))
    await add_rider(env, "second", at=km_north(1.5))
    delivery_id, _ = await assigned(env)
    await activities.release_rider(_release(delivery_id, RiderReleaseOutcome.DROPPED))

    assert (await _rider(env, "first")).status == "offline"
    assert not await _in_pool(env, "first")
    offer = await _offer(delivery_id, exclude=["first"])
    assert offer is not None and offer.rider_id == "second"
    assert (await _delivery(env, delivery_id)).reassignment_count == 1


async def test_full_delivery_then_rider_is_free_again(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    for status in ("picked_up", "delivered"):
        await activities.transition_delivery(
            TransitionDeliveryInput(delivery_id=delivery_id, to_status=status)  # type: ignore[arg-type]
        )
    await activities.release_rider(_release(delivery_id, RiderReleaseOutcome.COMPLETED))

    delivery = await _delivery(env, delivery_id)
    assert delivery.status == "delivered"
    assert delivery.rider_id == "near"  # who delivered it stays recorded
    assert delivery.assigned_at and delivery.picked_up_at and delivery.delivered_at
    assert (await _rider(env, "near")).status == "available"
    assert await _in_pool(env, "near")
    async with env.factory() as session:
        history = (
            (
                await session.execute(
                    select(DeliveryStatusHistory.to_status).order_by(
                        DeliveryStatusHistory.created_at
                    )
                )
            )
            .scalars()
            .all()
        )
        events = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert history == [
        "pending_assignment",
        "offered",
        "assigned",
        "picked_up",
        "delivered",
    ]
    assert events == 4  # one DeliveryStatusUpdated per change after creation


async def test_transition_is_idempotent_and_checked(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await activities.transition_delivery(
        TransitionDeliveryInput(delivery_id=delivery_id, to_status="assigned")
    )
    with pytest.raises(ApplicationError) as exc_info:
        await activities.transition_delivery(
            TransitionDeliveryInput(delivery_id=delivery_id, to_status="delivered")
        )
    assert exc_info.value.type == BusinessErrorType.INVALID_DELIVERY_STATE


# ---------- ETA ----------


async def test_eta_from_rider_position_and_food_ready_time(env: Env) -> None:
    await add_rider(env, "near", at=km_north(1), vehicle_type="bike")
    delivery_id, _ = await assigned(env)
    estimate = await activities.compute_eta(DeliveryRef(delivery_id))
    # Bike: 1 km × 1.3 / 12 km/h = 6.5 min to the restaurant; the 2.22 km
    # drop-off leg takes ~14.4 min.
    leg = estimate.dropoff_eta - estimate.pickup_eta
    assert leg == pytest.approx(timedelta(minutes=14.4), abs=timedelta(seconds=10))
    delivery = await _delivery(env, delivery_id)
    assert (delivery.pickup_eta, delivery.dropoff_eta) == (
        estimate.pickup_eta,
        estimate.dropoff_eta,
    )
    assert env.published[-1][1]["pickup_eta"] == estimate.pickup_eta.isoformat()


async def test_eta_waits_for_the_food(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.1))
    result = await activities.create_delivery(
        CreateDeliveryInput(
            order_id="o-1",
            customer_id=CUSTOMER,
            pickup=PICKUP,
            dropoff=DROPOFF,
            ready_at=(ready := datetime.now(UTC) + timedelta(minutes=25)),
        )
    )
    await _offer(result.delivery_id)
    estimate = await activities.compute_eta(DeliveryRef(result.delivery_id))
    assert estimate.pickup_eta == ready


# ---------- Create / cancel ----------


async def test_create_is_idempotent_per_order(env: Env) -> None:
    first = await new_delivery(env)
    assert await new_delivery(env) == first


async def test_cancel_releases_the_rider(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await activities.cancel_delivery(OrderRef("o-1"))
    await activities.cancel_delivery(OrderRef("o-1"))  # retry
    delivery = await _delivery(env, delivery_id)
    assert (delivery.status, delivery.cancelled_at is not None) == ("cancelled", True)
    assert (await _rider(env, "near")).status == "available"
    assert await _in_pool(env, "near")


async def test_no_cancel_after_pickup(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await activities.transition_delivery(
        TransitionDeliveryInput(delivery_id=delivery_id, to_status="picked_up")
    )
    with pytest.raises(ApplicationError) as exc_info:
        await activities.cancel_delivery(OrderRef("o-1"))
    assert exc_info.value.type == BusinessErrorType.INVALID_DELIVERY_STATE


async def test_cancel_before_create_blocks_a_late_create(env: Env) -> None:
    await activities.cancel_delivery(OrderRef("o-1"))
    result = await activities.create_delivery(
        CreateDeliveryInput(order_id="o-1", customer_id=CUSTOMER, pickup=PICKUP, dropoff=DROPOFF)
    )
    assert result.status == "cancelled"
