"""OrderSagaWorkflow part 1 (week2-plan step 4) against Temporal's
time-skipping test server, with every service's activities faked on their
real task queues. Timeouts (restaurant decision, retry backoff) are skipped
instantly."""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sfo_common.temporal import DATA_CONVERTER
from sfo_contracts import BusinessErrorType, OrderSagaSignal
from sfo_contracts import business_error as make_business_error
from sfo_contracts.activities import (
    DeliveryActivity,
    OrderActivity,
    PaymentActivity,
    RestaurantActivity,
)
from sfo_contracts.workflows import CancelRequest, RestaurantResponse
from temporalio.testing import WorkflowEnvironment

from tests.saga_fakes import (
    CUSTOMER,
    FORWARD,
    OWNER,
    UNDO_ALL,
    FakeServices,
    final_transition,
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


# ---------- Happy path ----------


async def test_accepted_order_is_confirmed_and_handed_to_delivery(
    env: WorkflowEnvironment,
) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input())
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.RESTAURANT_RESPONSE, RestaurantResponse(decision="accept", actor=OWNER)
        )
        await wait_for_step(handle, "offered:r1")

    assert fakes.names()[: len(FORWARD) + 2] == [
        *FORWARD,
        OrderActivity.TRANSITION_ORDER,  # → confirmed
        DeliveryActivity.CREATE_DELIVERY,
    ]
    pricing_in = fakes.arg(OrderActivity.APPLY_PRICING)
    assert (pricing_in.discount, pricing_in.tax, pricing_in.delivery_fee) == (
        Decimal("100.00"),
        Decimal("45.00"),
        Decimal("166.00"),
    )
    # Authorized for exactly the priced total.
    assert fakes.arg(PaymentActivity.AUTHORIZE).amount == Decimal("1111.00")
    confirmed = fakes.args(OrderActivity.TRANSITION_ORDER)[1]
    assert (confirmed.to_status, confirmed.actor) == ("confirmed", OWNER)


async def test_no_promo_code_skips_the_promotion(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input(promo_code=None))
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.RESTAURANT_RESPONSE, RestaurantResponse(decision="accept", actor=OWNER)
        )
        await wait_for_step(handle, "offered:r1")
    assert RestaurantActivity.REDEEM_PROMOTION not in fakes.names()
    assert fakes.arg(OrderActivity.APPLY_PRICING).discount == Decimal("0.00")


# ---------- Failures before the restaurant sees the order ----------


async def test_out_of_stock_rejects_and_releases_nothing_else(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.fail[RestaurantActivity.RESERVE_ORDER] = [
        make_business_error(BusinessErrorType.OUT_OF_STOCK, "sold out")
    ]
    async with running(env, fakes):
        handle = await start(env, saga_input())
        assert await handle.result() == "rejected"
    assert fakes.names() == [
        RestaurantActivity.RESERVE_ORDER,
        RestaurantActivity.RELEASE_RESERVATION,  # idempotent no-op; safe if it half-ran
        OrderActivity.TRANSITION_ORDER,
    ]
    rejected = final_transition(fakes)
    assert (rejected.to_status, rejected.reason) == ("rejected", "out_of_stock")


async def test_payment_declined_releases_promotion_and_stock(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.fail[PaymentActivity.AUTHORIZE] = [
        make_business_error(BusinessErrorType.PAYMENT_DECLINED, "declined")
    ]
    async with running(env, fakes):
        handle = await start(env, saga_input())
        assert await handle.result() == "rejected"
    assert fakes.names() == [*FORWARD[:4], *UNDO_ALL, OrderActivity.TRANSITION_ORDER]
    assert final_transition(fakes).reason == "payment_declined"


async def test_retries_exhausted_on_infrastructure_error_rejects(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.fail[PaymentActivity.AUTHORIZE] = [ConnectionError("payment-db down")] * 5
    async with running(env, fakes):
        handle = await start(env, saga_input())
        assert await handle.result() == "rejected"
    assert fakes.names().count(PaymentActivity.AUTHORIZE) == 5
    assert fakes.names()[-4:] == [*UNDO_ALL, OrderActivity.TRANSITION_ORDER]
    assert final_transition(fakes).reason == "system_error"


async def test_already_released_reservation_is_not_used(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    fakes.reserve_status = "released"
    async with running(env, fakes):
        handle = await start(env, saga_input())
        assert await handle.result() == "rejected"
    assert PaymentActivity.AUTHORIZE not in fakes.names()
    assert final_transition(fakes).reason == "reservation_released"


# ---------- Restaurant decision ----------


async def test_restaurant_rejection_voids_and_releases(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input())
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.RESTAURANT_RESPONSE,
            RestaurantResponse(decision="reject", reason="Kitchen closed", actor=OWNER),
        )
        assert await handle.result() == "rejected"
    assert fakes.names() == [*FORWARD, *UNDO_ALL, OrderActivity.TRANSITION_ORDER]
    rejected = final_transition(fakes)
    assert (rejected.reason, rejected.actor) == ("restaurant_rejected: Kitchen closed", OWNER)


async def test_no_restaurant_decision_times_out(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input())
        # Waiting on the result lets the test server skip the 5 minutes.
        assert await handle.result() == "rejected"
    assert fakes.names() == [*FORWARD, *UNDO_ALL, OrderActivity.TRANSITION_ORDER]
    assert final_transition(fakes).reason == "restaurant_timeout"


# ---------- Cancellation ----------


async def test_customer_cancel_while_awaiting_restaurant(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input())
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER, reason="Changed my mind")
        )
        assert await handle.result() == "cancelled"
    assert fakes.names() == [*FORWARD, *UNDO_ALL, OrderActivity.TRANSITION_ORDER]
    cancelled = final_transition(fakes)
    assert (cancelled.to_status, cancelled.reason, cancelled.actor) == (
        "cancelled",
        "Changed my mind",
        CUSTOMER,
    )


async def test_cancel_before_payment_undoes_only_what_ran(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    # Pricing keeps failing (retrying) until the cancel arrives.
    fakes.fail[OrderActivity.APPLY_PRICING] = [ConnectionError("order-db down")] * 3
    async with running(env, fakes):
        handle = await start(env, saga_input())
        await wait_for_step(handle, "pricing")
        await handle.signal(OrderSagaSignal.CANCEL, CancelRequest(actor=CUSTOMER))
        assert await handle.result() == "cancelled"
    names = fakes.names()
    assert PaymentActivity.AUTHORIZE not in names
    assert names[-3:] == [
        RestaurantActivity.RELEASE_PROMOTION,
        RestaurantActivity.RELEASE_RESERVATION,
        OrderActivity.TRANSITION_ORDER,
    ]
    assert final_transition(fakes).reason == "cancelled_by_customer"


# ---------- Compensation is retried until it succeeds ----------


async def test_failing_compensation_is_retried_until_it_succeeds(
    env: WorkflowEnvironment,
) -> None:
    fakes = FakeServices()
    fakes.fail[PaymentActivity.AUTHORIZE] = [
        make_business_error(BusinessErrorType.PAYMENT_DECLINED, "declined")
    ]
    fakes.fail[RestaurantActivity.RELEASE_RESERVATION] = [ConnectionError("blip")] * 2
    async with running(env, fakes):
        handle = await start(env, saga_input())
        assert await handle.result() == "rejected"
    assert fakes.names().count(RestaurantActivity.RELEASE_RESERVATION) == 3
    assert final_transition(fakes).reason == "payment_declined"
