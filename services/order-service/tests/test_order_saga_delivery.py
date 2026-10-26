"""OrderSagaWorkflow part 2 and OrderRefundWorkflow (week2-plan step 6)
against Temporal's time-skipping test server with faked activities: rider
assignment and reassignment, pickup, delivery and settlement, cancellation
after confirmation, post-delivery refund."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from app.workflows.refund import OrderRefundWorkflow
from sfo_common.temporal import DATA_CONVERTER
from sfo_contracts import OrderSagaSignal, TaskQueue, refund_workflow_id
from sfo_contracts.activities import (
    DeliveryActivity,
    OrderActivity,
    PaymentActivity,
    RestaurantActivity,
    RiderReleaseOutcome,
)
from sfo_contracts.workflows import (
    CancelRequest,
    OrderRefundInput,
    RestaurantResponse,
    RiderDropped,
    RiderResponse,
)
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from tests.saga_fakes import (
    CUSTOMER,
    DELIVERY_ID,
    OWNER,
    FakeServices,
    final_transition,
    order_transitions,
    running,
    saga_input,
    start,
    wait_for_step,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=DATA_CONVERTER) as env:
        yield env


async def confirmed(env: WorkflowEnvironment) -> WorkflowHandle:
    """A saga that the restaurant has accepted, now offering to its first rider."""
    handle = await start(env, saga_input())
    await wait_for_step(handle, "awaiting_restaurant")
    await handle.signal(
        OrderSagaSignal.RESTAURANT_RESPONSE, RestaurantResponse(decision="accept", actor=OWNER)
    )
    return handle


async def answer(handle: WorkflowHandle, rider: str, *, accepted: bool) -> None:
    await wait_for_step(handle, f"offered:{rider}")
    await handle.signal(
        OrderSagaSignal.RIDER_RESPONSE,
        RiderResponse(delivery_id=DELIVERY_ID, rider_id=rider, accepted=accepted),
    )


def released(fakes: FakeServices) -> list[str]:
    return [a.outcome for a in fakes.args(DeliveryActivity.RELEASE_RIDER)]


def offers_excluded(fakes: FakeServices) -> list[list[str]]:
    return [a.exclude_rider_ids for a in fakes.args(DeliveryActivity.OFFER_TO_NEAREST_RIDER)]


def search_radii(fakes: FakeServices) -> list[float | None]:
    return [a.radius_km for a in fakes.args(DeliveryActivity.OFFER_TO_NEAREST_RIDER)]


CANCEL_ALL = [
    DeliveryActivity.CANCEL_DELIVERY,
    PaymentActivity.VOID,
    RestaurantActivity.RELEASE_PROMOTION,
    RestaurantActivity.RELEASE_RESERVATION,
    OrderActivity.TRANSITION_ORDER,
]


# ---------- Full lifecycle ----------


async def test_order_is_delivered_and_settled(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.PICKED_UP)
        await wait_for_step(handle, "delivering")
        await handle.signal(OrderSagaSignal.DELIVERED)
        assert await handle.result() == "delivered"

    assert order_transitions(fakes) == [
        "awaiting_restaurant_acceptance",
        "confirmed",
        "assigned",
        "picked_up",
        "delivered",
    ]
    assert [a.to_status for a in fakes.args(DeliveryActivity.TRANSITION_DELIVERY)] == [
        "assigned",
        "picked_up",
        "delivered",
    ]
    # Capacity freed at pickup, payment captured once at delivery, rider freed.
    names = fakes.names()
    assert names.index(RestaurantActivity.COMPLETE_RESERVATION) < names.index(
        PaymentActivity.CAPTURE
    )
    assert names.count(PaymentActivity.CAPTURE) == 1
    assert released(fakes) == [RiderReleaseOutcome.COMPLETED]
    # Nothing undone.
    assert not {PaymentActivity.VOID, DeliveryActivity.CANCEL_DELIVERY} & set(names)
    created = fakes.arg(DeliveryActivity.CREATE_DELIVERY)
    assert created.ready_at is not None  # now + the restaurant's 20-minute prep time
    offer = fakes.arg(DeliveryActivity.OFFER_TO_NEAREST_RIDER)
    assert offer.offer_timeout_seconds == 60


# ---------- Reassignment ----------


async def test_declined_offer_goes_to_the_next_rider(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=False)
        await answer(handle, "r2", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
    assert released(fakes) == [RiderReleaseOutcome.DECLINED]
    assert offers_excluded(fakes) == [[], ["r1"]]
    assert order_transitions(fakes)[-1] == "assigned"


async def test_expired_offer_goes_to_the_next_rider(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await wait_for_step(handle, "offered:r1")
        await env.sleep(timedelta(seconds=61))  # r1 never answers
        await answer(handle, "r2", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
    assert released(fakes) == [RiderReleaseOutcome.EXPIRED]


async def test_late_answer_from_an_expired_rider_is_ignored(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await wait_for_step(handle, "offered:r1")
        await env.sleep(timedelta(seconds=61))
        await wait_for_step(handle, "offered:r2")
        # r1's accept arrives after its offer expired — it must not count as r2's.
        await handle.signal(
            OrderSagaSignal.RIDER_RESPONSE,
            RiderResponse(delivery_id=DELIVERY_ID, rider_id="r1", accepted=True),
        )
        await answer(handle, "r2", accepted=False)
        await answer(handle, "r3", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
    assert released(fakes) == [RiderReleaseOutcome.EXPIRED, RiderReleaseOutcome.DECLINED]


async def test_rider_dropping_out_is_replaced(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(
            OrderSagaSignal.RIDER_DROPPED,
            RiderDropped(delivery_id=DELIVERY_ID, rider_id="r1", reason="Flat tyre"),
        )
        await answer(handle, "r2", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.PICKED_UP)
        await wait_for_step(handle, "delivering")
        await handle.signal(OrderSagaSignal.DELIVERED)
        assert await handle.result() == "delivered"
    assert released(fakes) == [RiderReleaseOutcome.DROPPED, RiderReleaseOutcome.COMPLETED]
    assert offers_excluded(fakes) == [[], ["r1"]]
    # Order stays `assigned` across the replacement (the second is a no-op).
    assert order_transitions(fakes).count("assigned") == 2


async def test_drop_out_after_pickup_is_ignored(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.PICKED_UP)
        await wait_for_step(handle, "delivering")
        await handle.signal(
            OrderSagaSignal.RIDER_DROPPED,
            RiderDropped(delivery_id=DELIVERY_ID, rider_id="r1", reason="Tired"),
        )
        await handle.signal(OrderSagaSignal.DELIVERED)
        assert await handle.result() == "delivered"
    assert released(fakes) == [RiderReleaseOutcome.COMPLETED]


# ---------- No rider ----------


async def test_riders_who_declined_are_asked_again_after_two_minutes(
    env: WorkflowEnvironment,
) -> None:
    fakes = FakeServices()
    fakes.pool = ["r1", "r2"]  # the only riders nearby
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=False)
        await answer(handle, "r2", accepted=False)
        await wait_for_step(handle, "searching_rider")
        await env.sleep(timedelta(seconds=150))
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
    excluded = offers_excluded(fakes)
    assert excluded[:3] == [[], ["r1"], ["r1", "r2"]]
    # Nobody else came free; r1 was asked again once the 2 minutes were up.
    assert "r1" not in excluded[-1]
    assert released(fakes) == [RiderReleaseOutcome.DECLINED] * 2
    assert order_transitions(fakes)[-1] == "assigned"


async def test_riders_turning_it_down_for_ten_minutes_cancels_the_order(
    env: WorkflowEnvironment,
) -> None:
    fakes = FakeServices()
    fakes.pool = ["r1"]  # never answers; each offer expires
    async with running(env, fakes):
        handle = await confirmed(env)
        assert await handle.result() == "cancelled"
    released_outcomes = released(fakes)
    assert len(released_outcomes) >= 3
    assert set(released_outcomes) == {RiderReleaseOutcome.EXPIRED}
    assert fakes.names()[-5:] == CANCEL_ALL
    cancelled = final_transition(fakes)
    assert (cancelled.to_status, cancelled.reason) == ("cancelled", "no_rider_available")


async def test_nobody_free_for_ten_minutes_cancels_the_order(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.riders = []  # nobody available, ever
    async with running(env, fakes):
        handle = await confirmed(env)
        assert await handle.result() == "cancelled"
    # Looked again every 30s for 10 minutes, farther out each time.
    searches = search_radii(fakes)
    assert 20 <= len(searches) <= 21
    assert searches[:4] == [5, 8, 12, 12]
    assert final_transition(fakes).reason == "no_rider_available"


async def test_rider_found_after_a_wait(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.riders = [None, None, "r1"]  # free from the third look
    async with running(env, fakes):
        handle = await confirmed(env)
        await wait_for_step(handle, "searching_rider")
        await env.sleep(timedelta(seconds=61))
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
    assert fakes.names().count(DeliveryActivity.OFFER_TO_NEAREST_RIDER) == 3


# ---------- Failures after confirmation ----------


async def test_retries_exhausted_after_confirmation_cancels(env: WorkflowEnvironment) -> None:
    # A confirmed order can't become `rejected`: it ends `cancelled`.
    fakes = FakeServices()
    fakes.fail[DeliveryActivity.CREATE_DELIVERY] = [ConnectionError("delivery-db down")] * 5
    async with running(env, fakes):
        handle = await confirmed(env)
        assert await handle.result() == "cancelled"
    assert fakes.names()[-5:] == CANCEL_ALL
    cancelled = final_transition(fakes)
    assert (cancelled.to_status, cancelled.reason) == ("cancelled", "system_error")


# ---------- Cancellation after confirmation ----------


async def test_cancel_while_searching_for_a_rider(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await wait_for_step(handle, "offered:r1")
        await handle.signal(OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER))
        assert await handle.result() == "cancelled"
    # cancel_delivery also releases the rider holding the offer.
    assert fakes.names()[-5:] == CANCEL_ALL
    assert final_transition(fakes).reason == "cancelled_by_customer"


async def test_cancel_after_assignment(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(
            OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER, reason="Taking too long")
        )
        assert await handle.result() == "cancelled"
    assert fakes.names()[-5:] == CANCEL_ALL
    assert PaymentActivity.CAPTURE not in fakes.names()
    assert final_transition(fakes).reason == "Taking too long"


async def test_cancel_after_pickup_is_ignored(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.PICKED_UP)
        await handle.signal(OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER))
        await wait_for_step(handle, "delivering")
        await handle.signal(OrderSagaSignal.DELIVERED)
        assert await handle.result() == "delivered"
    assert PaymentActivity.VOID not in fakes.names()
    assert fakes.names().count(PaymentActivity.CAPTURE) == 1


async def test_pickup_after_cancel_is_ignored(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER))
        await handle.signal(OrderSagaSignal.PICKED_UP)
        assert await handle.result() == "cancelled"
    assert "picked_up" not in order_transitions(fakes)


# ---------- Settlement must succeed ----------


async def test_capture_is_retried_until_it_succeeds(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.fail[PaymentActivity.CAPTURE] = [ConnectionError("payment-db down")] * 7
    async with running(env, fakes):
        handle = await confirmed(env)
        await answer(handle, "r1", accepted=True)
        await wait_for_step(handle, "awaiting_pickup")
        await handle.signal(OrderSagaSignal.PICKED_UP)
        await wait_for_step(handle, "delivering")
        await handle.signal(OrderSagaSignal.DELIVERED)
        # More than the usual 5 attempts — no rollback after delivery.
        assert await handle.result() == "delivered"
    assert fakes.names().count(PaymentActivity.CAPTURE) == 8


# ---------- Post-delivery refund (D13) ----------


async def _refund(env: WorkflowEnvironment, amount: str) -> WorkflowHandle:
    order_id = f"o-{uuid.uuid4()}"
    return await env.client.start_workflow(
        OrderRefundWorkflow.run,
        OrderRefundInput(
            order_id=order_id, amount=Decimal(amount), reason="Cold food", actor=OWNER
        ),
        id=refund_workflow_id(order_id),
        task_queue=TaskQueue.ORDER_SAGA,
    )


async def test_refund_then_record_on_the_order(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await _refund(env, "250.50")
        result = await handle.result()
    assert result.refunded_amount == Decimal("250.50")
    refund = fakes.arg(PaymentActivity.REFUND)
    # The workflow's ID is the refund's idempotency key.
    assert refund.refund_id == handle.id
    recorded = fakes.arg(OrderActivity.RECORD_REFUND)
    assert (recorded.amount, recorded.actor) == (Decimal("250.50"), OWNER)


async def test_failed_refund_records_nothing(env: WorkflowEnvironment) -> None:
    from sfo_contracts import BusinessErrorType, business_error

    fakes = FakeServices()
    fakes.fail[PaymentActivity.REFUND] = [
        business_error(BusinessErrorType.REFUND_EXCEEDS_CAPTURED, "too much")
    ]
    async with running(env, fakes):
        handle = await _refund(env, "99999")
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert OrderActivity.RECORD_REFUND not in fakes.names()
