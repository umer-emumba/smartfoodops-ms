"""OrderSagaWorkflow part 1 (week2-plan step 4) against Temporal's
time-skipping test server, with every service's activities faked on their
real task queues. Timeouts (restaurant decision, retry backoff) are skipped
instantly."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from app.workflows.order_saga import OrderSagaWorkflow
from sfo_common.temporal import DATA_CONVERTER, WorkerSpec, run_workers
from sfo_contracts import BusinessErrorType, OrderSagaQuery, OrderSagaSignal, TaskQueue
from sfo_contracts import business_error as make_business_error
from sfo_contracts.activities import (
    Actor,
    ApplyPricingInput,
    ApplyPricingResult,
    AuthorizePaymentInput,
    GeoPoint,
    OrderActivity,
    OrderRef,
    PaymentActivity,
    PaymentResult,
    RedeemPromotionInput,
    RedeemPromotionResult,
    ReservedItem,
    ReserveOrderInput,
    ReserveOrderResult,
    RestaurantActivity,
    TransitionOrderInput,
    TransitionOrderResult,
)
from sfo_contracts.workflows import (
    CancelRequest,
    OrderSagaInput,
    PricingConfig,
    RestaurantResponse,
)
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.asyncio(loop_scope="module")

OWNER = Actor(id="u-owner", role="restaurant_admin")
CUSTOMER = Actor(id="u-customer", role="customer")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=DATA_CONVERTER) as env:
        yield env


class FakeServices:
    """Stand-ins for the restaurant/payment/order activities. Records every
    call; `fail[name]` is a list of exceptions raised by that activity's next
    calls (then it succeeds)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.reserve_status = "reserved"
        self.payment_status = "authorized"

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def arg(self, name: str) -> Any:
        return next(a for n, a in self.calls if n == name)

    def _record(self, name: str, arg: Any) -> None:
        self.calls.append((name, arg))
        pending = self.fail.get(name)
        if pending:
            raise pending.pop(0)

    def specs(self) -> list[WorkerSpec]:
        def reserve(data: ReserveOrderInput) -> ReserveOrderResult:
            return ReserveOrderResult(order_id=data.order_id, status=self.reserve_status)

        def redeem(_: RedeemPromotionInput) -> RedeemPromotionResult:
            return RedeemPromotionResult(
                promotion_id="p-1", discount_type="percentage", discount_value=Decimal("10")
            )

        def price(data: ApplyPricingInput) -> ApplyPricingResult:
            return ApplyPricingResult(
                total_amount=Decimal("1000") - data.discount + data.tax + data.delivery_fee
            )

        def authorize(data: AuthorizePaymentInput) -> PaymentResult:
            return PaymentResult(payment_id="pay-1", status=self.payment_status)

        def transition(data: TransitionOrderInput) -> TransitionOrderResult:
            return TransitionOrderResult(status=data.to_status, changed=True)

        def none(_: OrderRef) -> None:
            return None

        # The activity's declared input type drives deserialization.
        typed = {
            RestaurantActivity.RESERVE_ORDER: (reserve, ReserveOrderInput),
            RestaurantActivity.REDEEM_PROMOTION: (redeem, RedeemPromotionInput),
            RestaurantActivity.RELEASE_RESERVATION: (none, OrderRef),
            RestaurantActivity.RELEASE_PROMOTION: (none, OrderRef),
            PaymentActivity.AUTHORIZE: (authorize, AuthorizePaymentInput),
            PaymentActivity.VOID: (none, OrderRef),
            OrderActivity.APPLY_PRICING: (price, ApplyPricingInput),
            OrderActivity.TRANSITION_ORDER: (transition, TransitionOrderInput),
        }

        def build(name: str) -> Callable[..., Any]:
            result, input_type = typed[name]

            @activity.defn(name=name)
            async def run(arg: input_type) -> Any:  # type: ignore[valid-type]
                self._record(name, arg)
                return result(arg)

            return run

        return [
            WorkerSpec(TaskQueue.ORDER_SAGA, workflows=[OrderSagaWorkflow]),
            WorkerSpec(
                TaskQueue.RESTAURANT_ACTIVITIES,
                activities=[build(n) for n in typed if n in set(RestaurantActivity)],
            ),
            WorkerSpec(
                TaskQueue.PAYMENT_ACTIVITIES,
                activities=[build(n) for n in typed if n in set(PaymentActivity)],
            ),
            WorkerSpec(
                TaskQueue.ORDER_ACTIVITIES,
                activities=[build(n) for n in typed if n in set(OrderActivity)],
            ),
        ]


@asynccontextmanager
async def running(env: WorkflowEnvironment, fakes: FakeServices) -> AsyncIterator[None]:
    task = asyncio.create_task(run_workers(env.client, fakes.specs(), service_name="test"))
    try:
        yield
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def saga_input(promo_code: str | None = "WELCOME10") -> OrderSagaInput:
    return OrderSagaInput(
        order_id=f"o-{uuid.uuid4()}",
        customer_id="u-customer",
        restaurant_id="r-1",
        items=[ReservedItem(menu_item_id="m-1", quantity=2)],
        subtotal=Decimal("1000"),
        payment_method="card_simulated",
        pickup=GeoPoint(lat=33.6844, lng=73.0479),
        dropoff=GeoPoint(lat=33.7044, lng=73.0479),
        pricing=PricingConfig(
            tax_rate=Decimal("0.05"), base_delivery_fee=Decimal("100"), per_km_fee=Decimal("30")
        ),
        restaurant_accept_timeout_seconds=300,
        promo_code=promo_code,
    )


async def start(env: WorkflowEnvironment, data: OrderSagaInput) -> WorkflowHandle:
    return await env.client.start_workflow(
        OrderSagaWorkflow.run, data, id=f"order-{data.order_id}", task_queue=TaskQueue.ORDER_SAGA
    )


async def wait_for_step(handle: WorkflowHandle, step: str) -> None:
    for _ in range(200):
        if await handle.query(OrderSagaQuery.STATUS) == step:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"saga never reached {step}")


def final_transition(fakes: FakeServices) -> TransitionOrderInput:
    return [a for n, a in fakes.calls if n == OrderActivity.TRANSITION_ORDER][-1]


FORWARD = [
    RestaurantActivity.RESERVE_ORDER,
    RestaurantActivity.REDEEM_PROMOTION,
    OrderActivity.APPLY_PRICING,
    PaymentActivity.AUTHORIZE,
    OrderActivity.TRANSITION_ORDER,  # → awaiting_restaurant_acceptance
]
UNDO_ALL = [
    PaymentActivity.VOID,
    RestaurantActivity.RELEASE_PROMOTION,
    RestaurantActivity.RELEASE_RESERVATION,
]


# ---------- Happy path ----------


async def test_accepted_order_is_confirmed(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input())
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.RESTAURANT_RESPONSE, RestaurantResponse(decision="accept", actor=OWNER)
        )
        assert await handle.result() == "confirmed"

    assert fakes.names() == [*FORWARD, OrderActivity.TRANSITION_ORDER]
    pricing_in = fakes.arg(OrderActivity.APPLY_PRICING)
    assert (pricing_in.discount, pricing_in.tax, pricing_in.delivery_fee) == (
        Decimal("100.00"),
        Decimal("45.00"),
        Decimal("166.00"),
    )
    # Authorized for exactly the priced total.
    assert fakes.arg(PaymentActivity.AUTHORIZE).amount == Decimal("1111.00")
    confirmed = final_transition(fakes)
    assert (confirmed.to_status, confirmed.actor) == ("confirmed", OWNER)


async def test_no_promo_code_skips_the_promotion(env: WorkflowEnvironment) -> None:
    fakes = FakeServices()
    async with running(env, fakes):
        handle = await start(env, saga_input(promo_code=None))
        await wait_for_step(handle, "awaiting_restaurant")
        await handle.signal(
            OrderSagaSignal.RESTAURANT_RESPONSE, RestaurantResponse(decision="accept", actor=OWNER)
        )
        await handle.result()
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
