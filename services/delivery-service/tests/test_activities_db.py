"""delivery-activities against a real Postgres and Redis (week2-plan step 5)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app import activities, redis_client, rider_locations
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
    outbox_payloads,
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
    # The rider's live feed and notification-service learn of it from the event.
    offered_event = (await outbox_payloads(env, "DeliveryStatusUpdated"))[-1]
    assert (offered_event["to_status"], offered_event["rider_user_id"]) == ("offered", "user-near")


async def test_riders_beyond_the_search_radius_are_not_offered(env: Env) -> None:
    await add_rider(env, "too-far", at=km_north(6))
    delivery_id = await new_delivery(env)
    assert await _offer(delivery_id) is None
    assert (await _delivery(env, delivery_id)).status == "pending_assignment"
    assert (await _rider(env, "too-far")).status == "available"


async def test_the_sagas_wider_radius_reaches_farther_riders(env: Env) -> None:
    await add_rider(env, "farther", at=km_north(6))
    delivery_id = await new_delivery(env)
    offer = await activities.offer_to_nearest_rider(
        OfferRiderInput(delivery_id=delivery_id, radius_km=8)
    )
    assert offer is not None and offer.rider_id == "farther"


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


@pytest.mark.parametrize(
    "outcome", [RiderReleaseOutcome.DECLINED, RiderReleaseOutcome.EXPIRED, RiderReleaseOutcome.LATE]
)
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
    # Dropping out ends the shift (week3-plan W3-D8c); being claimed doesn't.
    [shift] = await outbox_payloads(env, "RiderShiftChanged")
    assert (shift["rider_id"], shift["rider_user_id"]) == ("first", "user-first")
    assert (shift["from_status"], shift["to_status"]) == ("busy", "offline")
    assert (shift["on_shift"], shift["reason"]) == (False, "dropped_out")


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
    assert events == 5  # one DeliveryStatusUpdated per change, creation included
    # Enriched with the recipients (week3-plan W3-D8).
    updates = await outbox_payloads(env, "DeliveryStatusUpdated")
    assert [(u["from_status"], u["to_status"]) for u in updates] == [
        (None, "pending_assignment"),
        ("pending_assignment", "offered"),
        ("offered", "assigned"),
        ("assigned", "picked_up"),
        ("picked_up", "delivered"),
    ]
    assert {(u["rider_user_id"], u["customer_id"]) for u in updates[1:]} == {
        ("user-near", CUSTOMER)
    }


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
    # The customer's live feed shows them from this event (no status change).
    eta_event = (await outbox_payloads(env, "DeliveryEtaUpdated"))[-1]
    assert (eta_event["status"], eta_event["rider_id"]) == ("assigned", "near")
    assert datetime.fromisoformat(eta_event["pickup_eta"]) == estimate.pickup_eta


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


# ---------- Food ready ----------


async def test_food_ready_brings_the_assigned_riders_eta_forward(env: Env) -> None:
    await add_rider(env, "near", at=km_north(0.1))
    delivery_id, _ = await assigned(env)
    async with env.factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        assert delivery is not None
        delivery.ready_at = datetime.now(UTC) + timedelta(minutes=30)  # due later
        await session.commit()
    await activities.compute_eta(DeliveryRef(delivery_id))
    due = (await _delivery(env, delivery_id)).pickup_eta
    new_eta = await activities.mark_food_ready(DeliveryRef(delivery_id))
    # Retry / second tap: no-op.
    assert await activities.mark_food_ready(DeliveryRef(delivery_id)) is None

    delivery = await _delivery(env, delivery_id)
    assert new_eta is not None and new_eta.pickup_eta == delivery.pickup_eta
    assert delivery.food_ready_at is not None and delivery.ready_at == delivery.food_ready_at
    assert due is not None and delivery.pickup_eta is not None and delivery.pickup_eta < due
    [event] = await outbox_payloads(env, "DeliveryFoodReady")
    assert (event["status"], event["rider_id"], event["rider_user_id"]) == (
        "assigned",
        "near",
        "user-near",
    )
    assert datetime.fromisoformat(event["pickup_eta"]) == delivery.pickup_eta


async def test_food_ready_before_a_rider_has_no_one_to_tell(env: Env) -> None:
    delivery_id = await new_delivery(env)
    assert await activities.mark_food_ready(DeliveryRef(delivery_id)) is None
    [event] = await outbox_payloads(env, "DeliveryFoodReady")
    assert (event["status"], event["rider_user_id"], event["pickup_eta"]) == (
        "pending_assignment",
        None,
        None,
    )


async def test_food_ready_after_cancel_is_a_no_op(env: Env) -> None:
    delivery_id = await new_delivery(env)
    await activities.cancel_delivery(OrderRef("o-1"))
    await activities.mark_food_ready(DeliveryRef(delivery_id))
    assert (await _delivery(env, delivery_id)).food_ready_at is None
    assert await outbox_payloads(env, "DeliveryFoodReady") == []


async def test_create_is_idempotent_per_order(env: Env) -> None:
    first = await new_delivery(env)
    assert await new_delivery(env) == first


async def test_a_released_rider_is_named_in_the_event(env: Env) -> None:
    """The event names who lost the offer, so their live feed can be told:
    rider_user_id is already None by then."""
    await add_rider(env, "near", at=km_north(0.5))
    delivery_id, _ = await offered(env)
    await activities.release_rider(
        ReleaseRiderInput(delivery_id=delivery_id, outcome=RiderReleaseOutcome.DECLINED)
    )
    released = (await outbox_payloads(env, "DeliveryStatusUpdated"))[-1]
    assert (released["to_status"], released["reason"]) == ("pending_assignment", "rider_declined")
    assert (released["rider_user_id"], released["released_rider_user_id"]) == (None, "user-near")


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


async def test_location_pings_write_the_order_tracking_state(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only positions go to Redis from here; delivery status and ETAs reach
    the state through their events (tracking-service)."""
    from app import tracking

    # The real publisher this time, against the test Redis.
    monkeypatch.undo()
    monkeypatch.setattr(activities, "SessionFactory", env.factory)
    monkeypatch.setattr(redis_client, "redis", env.redis)
    await add_rider(env, "near", at=km_north(0.5))
    await assigned(env)
    await tracking.publish_location("o-1", "near", 24.818, 67.03)

    state = await env.redis.hgetall("tracking:order:o-1:state")
    assert state["rider_lat"] == "24.818" and state["rider_location_at"]
    assert "delivery_status" not in state
    assert 0 < await env.redis.ttl("tracking:order:o-1:state") <= 24 * 3600
