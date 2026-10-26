"""Fakes for the order-saga tests: every activity the saga calls, on its
real task queue, recording calls. Run against Temporal's time-skipping test
server (`env` fixture in each test module)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.workflows.order_saga import OrderSagaWorkflow
from app.workflows.refund import OrderRefundWorkflow
from sfo_common.temporal import WorkerSpec, run_workers
from sfo_contracts import OrderSagaQuery, TaskQueue
from sfo_contracts.activities import (
    Actor,
    ApplyPricingInput,
    ApplyPricingResult,
    AuthorizePaymentInput,
    CreateDeliveryInput,
    CreateDeliveryResult,
    DeliveryActivity,
    DeliveryEta,
    DeliveryRef,
    GeoPoint,
    OfferRiderInput,
    OrderActivity,
    OrderRef,
    PaymentActivity,
    PaymentResult,
    RecordRefundInput,
    RedeemPromotionInput,
    RedeemPromotionResult,
    RefundPaymentInput,
    RefundPaymentResult,
    ReleaseRiderInput,
    ReservedItem,
    ReserveOrderInput,
    ReserveOrderResult,
    RestaurantActivity,
    RiderOffer,
    TransitionDeliveryInput,
    TransitionOrderInput,
    TransitionOrderResult,
)
from sfo_contracts.workflows import OrderSagaInput, PricingConfig
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

OWNER = Actor(id="u-owner", role="restaurant_admin")
CUSTOMER = Actor(id="u-customer", role="customer")
DELIVERY_ID = "d-1"


class FakeServices:
    """Stand-ins for every service's activities. Records each call;
    `fail[name]` is a list of exceptions raised by that activity's next calls
    (then it succeeds). `riders` is who each offer goes to, in order (None =
    nobody free; an empty list = nobody free from then on). Setting `pool`
    instead models riders who stay free: each offer goes to the first one not
    excluded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.reserve_status = "reserved"
        self.payment_status = "authorized"
        self.delivery_status = "pending_assignment"
        self.riders: list[str | None] = ["r1", "r2", "r3", "r4", "r5", "r6"]
        self.pool: list[str] | None = None

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, name: str) -> list[Any]:
        return [a for n, a in self.calls if n == name]

    def arg(self, name: str) -> Any:
        return self.args(name)[0]

    def _record(self, name: str, arg: Any) -> None:
        self.calls.append((name, arg))
        pending = self.fail.get(name)
        if pending:
            raise pending.pop(0)

    def _offer(self, data: OfferRiderInput) -> RiderOffer | None:
        if self.pool is not None:
            rider = next((r for r in self.pool if r not in data.exclude_rider_ids), None)
        else:
            rider = self.riders.pop(0) if self.riders else None
        if rider is None:
            return None
        return RiderOffer(
            rider_id=rider,
            distance_km=0.5,
            expires_at=activity.info().current_attempt_scheduled_time + timedelta(seconds=60),
        )

    def specs(self) -> list[WorkerSpec]:
        R, P, Od, D = RestaurantActivity, PaymentActivity, OrderActivity, DeliveryActivity  # noqa: N806
        behaviours: dict[str, tuple[Callable[[Any], Any], type]] = {
            R.RESERVE_ORDER: (
                lambda d: ReserveOrderResult(
                    order_id=d.order_id, status=self.reserve_status, prep_time_minutes=20
                ),
                ReserveOrderInput,
            ),
            R.REDEEM_PROMOTION: (
                lambda _: RedeemPromotionResult(
                    promotion_id="p-1", discount_type="percentage", discount_value=Decimal("10")
                ),
                RedeemPromotionInput,
            ),
            R.RELEASE_RESERVATION: (lambda _: None, OrderRef),
            R.RELEASE_PROMOTION: (lambda _: None, OrderRef),
            R.COMPLETE_RESERVATION: (lambda _: None, OrderRef),
            P.AUTHORIZE: (
                lambda _: PaymentResult(payment_id="pay-1", status=self.payment_status),
                AuthorizePaymentInput,
            ),
            P.VOID: (lambda _: PaymentResult(payment_id="pay-1", status="voided"), OrderRef),
            P.CAPTURE: (lambda _: PaymentResult(payment_id="pay-1", status="captured"), OrderRef),
            P.REFUND: (
                lambda d: RefundPaymentResult(
                    payment_id="pay-1",
                    refunded_amount=d.amount,
                    total_refunded=d.amount,
                ),
                RefundPaymentInput,
            ),
            Od.APPLY_PRICING: (
                lambda d: ApplyPricingResult(
                    total_amount=Decimal("1000") - d.discount + d.tax + d.delivery_fee
                ),
                ApplyPricingInput,
            ),
            Od.TRANSITION_ORDER: (
                lambda d: TransitionOrderResult(status=d.to_status, changed=True),
                TransitionOrderInput,
            ),
            Od.RECORD_REFUND: (lambda _: None, RecordRefundInput),
            D.CREATE_DELIVERY: (
                lambda _: CreateDeliveryResult(
                    delivery_id=DELIVERY_ID, status=self.delivery_status
                ),
                CreateDeliveryInput,
            ),
            D.OFFER_TO_NEAREST_RIDER: (self._offer, OfferRiderInput),
            D.RELEASE_RIDER: (lambda _: None, ReleaseRiderInput),
            D.TRANSITION_DELIVERY: (lambda _: None, TransitionDeliveryInput),
            D.COMPUTE_ETA: (
                lambda _: DeliveryEta(
                    pickup_eta=activity.info().current_attempt_scheduled_time,
                    dropoff_eta=activity.info().current_attempt_scheduled_time,
                ),
                DeliveryRef,
            ),
            D.CANCEL_DELIVERY: (lambda _: None, OrderRef),
        }

        def build(name: str) -> Callable[..., Any]:
            result, input_type = behaviours[name]

            @activity.defn(name=name)
            async def run(arg: input_type) -> Any:  # type: ignore[valid-type]
                self._record(name, arg)
                return result(arg)

            return run

        def queue(enum: Any) -> list[Callable[..., Any]]:
            return [build(n) for n in behaviours if n in set(enum)]

        return [
            WorkerSpec(TaskQueue.ORDER_SAGA, workflows=[OrderSagaWorkflow, OrderRefundWorkflow]),
            WorkerSpec(TaskQueue.RESTAURANT_ACTIVITIES, activities=queue(R)),
            WorkerSpec(TaskQueue.PAYMENT_ACTIVITIES, activities=queue(P)),
            WorkerSpec(TaskQueue.ORDER_ACTIVITIES, activities=queue(Od)),
            WorkerSpec(TaskQueue.DELIVERY_ACTIVITIES, activities=queue(D)),
        ]


# Workflows started by the current test. A test that stops mid-saga must not
# leave its workflow running: the next test's time skipping would wake it up
# on that test's workers (and fakes).
_started: list[WorkflowHandle] = []


@asynccontextmanager
async def running(env: WorkflowEnvironment, fakes: FakeServices) -> AsyncIterator[None]:
    task = asyncio.create_task(run_workers(env.client, fakes.specs(), service_name="test"))
    try:
        yield
    finally:
        for handle in _started:
            with contextlib.suppress(RPCError):  # already finished
                await handle.terminate("test over")
        _started.clear()
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
    handle = await env.client.start_workflow(
        OrderSagaWorkflow.run, data, id=f"order-{data.order_id}", task_queue=TaskQueue.ORDER_SAGA
    )
    _started.append(handle)
    return handle


async def wait_for_step(handle: WorkflowHandle, step: str) -> None:
    for _ in range(250):
        if await handle.query(OrderSagaQuery.STATUS) == step:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"saga never reached {step}: at {await handle.query(OrderSagaQuery.STATUS)}"
    )


def order_transitions(fakes: FakeServices) -> list[str]:
    return [a.to_status for a in fakes.args(OrderActivity.TRANSITION_ORDER)]


def final_transition(fakes: FakeServices) -> TransitionOrderInput:
    return fakes.args(OrderActivity.TRANSITION_ORDER)[-1]


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
