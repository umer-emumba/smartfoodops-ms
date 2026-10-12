"""OrderSagaWorkflow, part 1 (docs/week2-plan.md §2 steps 1–7, step 4):

    reserve stock + capacity → redeem promotion → price → authorize payment
    → awaiting_restaurant_acceptance → wait for the restaurant (or cancel,
    or timeout) → confirmed

On any failure, rejection, timeout or cancellation, the steps already taken
are undone in reverse order (void payment, release promotion, release
reservation) and the order ends `rejected` or `cancelled` with a
`status_reason`.

Each forward step registers its compensation *before* it runs: a step can
commit and still report failure (a timeout after the commit), so its
compensation must run either way. That's safe because every compensation is
idempotent and leaves a placeholder when there was nothing to undo, which
also stops a late-arriving forward step (restaurant/payment activities).

Compensations and the final status change are retried until they succeed:
once the saga has decided to undo, giving up would leave stock held or
money authorized.

Delivery (§2 steps 8–14) is added in step 6; until then the saga ends at
`confirmed`."""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from sfo_contracts import (
        ACTIVITY_RETRY_POLICY,
        BusinessErrorType,
        OrderSagaQuery,
        OrderSagaSignal,
        TaskQueue,
        WorkflowName,
    )
    from sfo_contracts.activities import (
        SYSTEM,
        Actor,
        ApplyPricingInput,
        ApplyPricingResult,
        AuthorizePaymentInput,
        OrderActivity,
        OrderRef,
        PaymentActivity,
        PaymentResult,
        RedeemPromotionInput,
        RedeemPromotionResult,
        ReserveOrderInput,
        ReserveOrderResult,
        RestaurantActivity,
        TransitionOrderInput,
        TransitionOrderResult,
    )
    from sfo_contracts.workflows import CancelRequest, OrderSagaInput, RestaurantResponse

    from app import pricing
    from app.models import OrderStatus

ACTIVITY_TIMEOUT = timedelta(seconds=10)

# Unlimited attempts, backing off to one per minute.
COMPENSATION_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
)

# `orders.status_reason` for each business failure.
FAILURE_REASONS: dict[str, str] = {
    BusinessErrorType.OUT_OF_STOCK: "out_of_stock",
    BusinessErrorType.CAPACITY_FULL: "restaurant_at_capacity",
    BusinessErrorType.RESTAURANT_UNAVAILABLE: "restaurant_unavailable",
    BusinessErrorType.PROMOTION_INVALID: "promotion_invalid",
    BusinessErrorType.PAYMENT_DECLINED: "payment_declined",
}


class _Cancelled(Exception):
    def __init__(self, request: CancelRequest) -> None:
        super().__init__("cancelled")
        self.request = request


class _Failed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def failure_reason(error: ActivityError) -> str:
    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.type in FAILURE_REASONS:
        return FAILURE_REASONS[cause.type]
    # Retries exhausted on an infrastructure error, or an unexpected one.
    return "system_error"


def cancel_reason(request: CancelRequest) -> str:
    return request.reason or f"cancelled_by_{request.actor.role or 'system'}"


@workflow.defn(name=WorkflowName.ORDER_SAGA)
class OrderSagaWorkflow:
    def __init__(self) -> None:
        self._step = "starting"
        self._order_id = ""
        self._restaurant_response: RestaurantResponse | None = None
        self._cancel: CancelRequest | None = None
        # (activity, task queue) — every compensation takes OrderRef.
        self._compensations: list[tuple[str, str]] = []

    # ---------- Signals and query ----------

    @workflow.signal(name=OrderSagaSignal.RESTAURANT_RESPONSE)
    def restaurant_response(self, response: RestaurantResponse) -> None:
        if self._restaurant_response is None:  # the first response counts
            self._restaurant_response = response

    @workflow.signal(name=OrderSagaSignal.CANCEL)
    def cancel(self, request: CancelRequest) -> None:
        if self._cancel is None:
            self._cancel = request

    @workflow.query(name=OrderSagaQuery.STATUS)
    def status(self) -> str:
        """The saga's current step — for debugging (Temporal UI, CLI)."""
        return self._step

    # ---------- Helpers ----------

    async def _activity(
        self,
        name: str,
        arg: Any,
        task_queue: str,
        result_type: type | None = None,
        retry_policy: RetryPolicy = ACTIVITY_RETRY_POLICY,
    ) -> Any:
        return await workflow.execute_activity(
            name,
            arg,
            task_queue=task_queue,
            result_type=result_type,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=retry_policy,
        )

    async def _transition(
        self,
        status: OrderStatus,
        *,
        actor: Actor = SYSTEM,
        reason: str | None = None,
        retry_policy: RetryPolicy = ACTIVITY_RETRY_POLICY,
    ) -> None:
        await self._activity(
            OrderActivity.TRANSITION_ORDER,
            TransitionOrderInput(
                order_id=self._order_id, to_status=status, actor=actor, reason=reason
            ),
            TaskQueue.ORDER_ACTIVITIES,
            TransitionOrderResult,
            retry_policy,
        )

    def _check_cancelled(self) -> None:
        if self._cancel is not None:
            raise _Cancelled(self._cancel)

    async def _undo_and_finish(self, status: OrderStatus, reason: str, actor: Actor) -> str:
        self._step = "compensating"
        for name, task_queue in reversed(self._compensations):
            await self._activity(
                name,
                OrderRef(self._order_id),
                task_queue,
                retry_policy=COMPENSATION_RETRY_POLICY,
            )
        self._compensations.clear()
        await self._transition(
            status, actor=actor, reason=reason, retry_policy=COMPENSATION_RETRY_POLICY
        )
        self._step = status
        workflow.logger.info("Order %s ended %s (%s)", self._order_id, status, reason)
        return status

    # ---------- The saga ----------

    @workflow.run
    async def run(self, data: OrderSagaInput) -> str:
        self._order_id = data.order_id
        try:
            return await self._place_order(data)
        except _Cancelled as cancelled:
            request = cancelled.request
            return await self._undo_and_finish(
                OrderStatus.CANCELLED, cancel_reason(request), request.actor
            )
        except _Failed as failed:
            return await self._undo_and_finish(OrderStatus.REJECTED, failed.reason, SYSTEM)
        except ActivityError as error:
            return await self._undo_and_finish(OrderStatus.REJECTED, failure_reason(error), SYSTEM)

    async def _place_order(self, data: OrderSagaInput) -> str:
        self._check_cancelled()

        # 1. Stock + capacity (atomic in restaurant-service).
        self._step = "reserving"
        self._compensations.append(
            (RestaurantActivity.RELEASE_RESERVATION, TaskQueue.RESTAURANT_ACTIVITIES)
        )
        reservation: ReserveOrderResult = await self._activity(
            RestaurantActivity.RESERVE_ORDER,
            ReserveOrderInput(
                order_id=data.order_id, restaurant_id=data.restaurant_id, items=data.items
            ),
            TaskQueue.RESTAURANT_ACTIVITIES,
            ReserveOrderResult,
        )
        if reservation.status != "reserved":
            # Already released — only if this order was compensated before.
            raise _Failed("reservation_released")
        self._check_cancelled()

        # 2. Promotion.
        promotion: RedeemPromotionResult | None = None
        if data.promo_code:
            self._step = "redeeming_promotion"
            self._compensations.append(
                (RestaurantActivity.RELEASE_PROMOTION, TaskQueue.RESTAURANT_ACTIVITIES)
            )
            promotion = await self._activity(
                RestaurantActivity.REDEEM_PROMOTION,
                RedeemPromotionInput(
                    order_id=data.order_id, restaurant_id=data.restaurant_id, code=data.promo_code
                ),
                TaskQueue.RESTAURANT_ACTIVITIES,
                RedeemPromotionResult,
            )
            self._check_cancelled()

        # 3. Pricing.
        self._step = "pricing"
        discount = pricing.discount(data.subtotal, promotion)
        priced: ApplyPricingResult = await self._activity(
            OrderActivity.APPLY_PRICING,
            ApplyPricingInput(
                order_id=data.order_id,
                discount=discount,
                tax=pricing.tax(data.subtotal, discount, data.pricing),
                delivery_fee=pricing.delivery_fee(data.pickup, data.dropoff, data.pricing),
            ),
            TaskQueue.ORDER_ACTIVITIES,
            ApplyPricingResult,
        )
        self._check_cancelled()

        # 4. Payment authorization (captured on delivery, step 6).
        self._step = "authorizing_payment"
        self._compensations.append((PaymentActivity.VOID, TaskQueue.PAYMENT_ACTIVITIES))
        payment: PaymentResult = await self._activity(
            PaymentActivity.AUTHORIZE,
            AuthorizePaymentInput(
                order_id=data.order_id,
                customer_id=data.customer_id,
                amount=priced.total_amount,
                method=data.payment_method,
            ),
            TaskQueue.PAYMENT_ACTIVITIES,
            PaymentResult,
        )
        if payment.status != "authorized":
            raise _Failed("payment_voided")
        self._check_cancelled()

        # 5–6. Hand the order to the restaurant and wait for its decision.
        await self._transition(OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
        self._step = "awaiting_restaurant"
        try:
            await workflow.wait_condition(
                lambda: self._restaurant_response is not None or self._cancel is not None,
                timeout=timedelta(seconds=data.restaurant_accept_timeout_seconds),
            )
        except TimeoutError:
            raise _Failed("restaurant_timeout") from None
        self._check_cancelled()
        response = self._restaurant_response
        assert response is not None
        if response.decision == "reject":
            reason = "restaurant_rejected"
            if response.reason:
                reason = f"{reason}: {response.reason}"
            return await self._undo_and_finish(OrderStatus.REJECTED, reason, response.actor)

        # 7. Confirmed.
        await self._transition(OrderStatus.CONFIRMED, actor=response.actor)
        self._step = OrderStatus.CONFIRMED
        return OrderStatus.CONFIRMED
