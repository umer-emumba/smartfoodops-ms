"""OrderSagaWorkflow — one per order, the whole lifecycle
(docs/week2-plan.md §2):

    reserve stock + capacity → redeem promotion → price → authorize payment
    → awaiting_restaurant_acceptance → restaurant accepts → confirmed
    → create delivery → offer to the nearest rider (decline / expiry → next
      rider, decliners asked again after 2 minutes, search radius widening;
      up to 10 minutes) → assigned
    → picked_up (capacity freed) → delivered → payment captured

Until pickup, a failure, rejection, timeout, cancellation or "no rider"
undoes the steps already taken in reverse order (cancel delivery — which
releases the rider —, void payment, release promotion, release reservation)
and the order ends `rejected` (before confirmation) or `cancelled` with a
`status_reason`. A rider dropping out after accepting is replaced (D12); the
order stays `assigned`.

The restaurant marks the food ready (`food_ready` signal, any time after
confirmation; delivery-service records it and tells the customer and rider).
Pickup has deadlines, so a late kitchen and a late rider are told apart:
food not marked ready `food_overdue` after it was due (confirmation + prep
time) is flagged, and `kitchen_timeout` after it the order is cancelled
(`kitchen_timeout`); food that is ready but not picked up `pickup_grace`
after the later of "ready" and the rider's pickup ETA gets another rider.

After pickup nothing is undone — the food is on its way. Cancel and
drop-out are ignored from then on (the endpoints already refuse them), and
the remaining steps are retried until they succeed. An order not delivered
`delivery_overdue` after pickup is flagged (step `delivery_overdue`, a log
line, the `order_saga_overdue` metric); a system_admin can mark it
delivered.

Each forward step registers its compensation *before* it runs: a step can
commit and still report failure (a timeout after the commit), so its
compensation must run either way. That's safe because every compensation is
idempotent and leaves a placeholder when there was nothing to undo, which
also stops a late-arriving forward step.

Signals that arrive together are applied in arrival order: a cancel after
pickup, or a pickup after a cancel, is ignored."""

from collections.abc import Callable
from datetime import datetime, timedelta
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
        CreateDeliveryInput,
        CreateDeliveryResult,
        DeliveryActivity,
        DeliveryEta,
        DeliveryRef,
        OfferRiderInput,
        OrderActivity,
        OrderRef,
        PaymentActivity,
        PaymentResult,
        RedeemPromotionInput,
        RedeemPromotionResult,
        ReleaseRiderInput,
        ReserveOrderInput,
        ReserveOrderResult,
        RestaurantActivity,
        RiderOffer,
        RiderReleaseOutcome,
        TransitionDeliveryInput,
        TransitionOrderInput,
        TransitionOrderResult,
    )
    from sfo_contracts.workflows import (
        CancelRequest,
        OrderSagaInput,
        RestaurantResponse,
        RiderDropped,
        RiderResponse,
    )

    from app import pricing
    from app.models import OrderStatus

ACTIVITY_TIMEOUT = timedelta(seconds=10)

# Unlimited attempts, backing off to one per minute: compensations, and
# every step after pickup, must eventually succeed.
COMPENSATION_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
)
MUST_SUCCEED = COMPENSATION_RETRY_POLICY

# workflow.patched IDs: sagas started before a change replay the old code
# path, new ones take the new one.
PICKUP_DEADLINES = "pickup-deadlines"
DELIVERY_OVERDUE = "delivery-overdue"

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
    """Ends the order `status` (rejected, or cancelled for no rider)."""

    def __init__(
        self, reason: str, *, status: OrderStatus = OrderStatus.REJECTED, actor: Actor = SYSTEM
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.actor = actor


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
        # Set once the order may be `confirmed`: it can't be rejected from then on.
        self._confirmed = False
        self._restaurant_response: RestaurantResponse | None = None
        self._cancel: CancelRequest | None = None
        # (activity, task queue) — every compensation takes OrderRef.
        self._compensations: list[tuple[str, str]] = []
        # Delivery.
        self._delivery_id: str | None = None
        self._offered_rider: str | None = None  # waiting for this rider's answer
        self._rider_response: RiderResponse | None = None
        self._assigned_rider: str | None = None
        self._dropped: RiderDropped | None = None
        self._food_ready_signalled = False
        self._food_ready_at: datetime | None = None  # once delivery-service has it
        self._food_flagged = False  # flagged food_overdue already
        self._pickup_eta: datetime | None = None  # the assigned rider's latest
        self._picked_up = False
        self._delivered = False
        self._delivered_by: Actor | None = None  # a system_admin, not the rider
        self._finished = False

    # ---------- Signals and query ----------

    @workflow.signal(name=OrderSagaSignal.RESTAURANT_RESPONSE)
    def restaurant_response(self, response: RestaurantResponse) -> None:
        if self._restaurant_response is None:  # the first response counts
            self._restaurant_response = response

    @workflow.signal(name=OrderSagaSignal.CANCEL)
    def cancel(self, request: CancelRequest) -> None:
        # Not after pickup: the food is with the rider.
        if self._cancel is None and not self._picked_up:
            self._cancel = request

    @workflow.signal(name=OrderSagaSignal.RIDER_RESPONSE)
    def rider_response(self, response: RiderResponse) -> None:
        # Only the answer to the open offer counts — not a late one from a
        # rider whose offer already expired.
        if (
            self._rider_response is None
            and response.delivery_id == self._delivery_id
            and response.rider_id == self._offered_rider
        ):
            self._rider_response = response

    @workflow.signal(name=OrderSagaSignal.RIDER_DROPPED)
    def rider_dropped(self, dropped: RiderDropped) -> None:
        if (
            self._dropped is None
            and not self._picked_up
            and dropped.delivery_id == self._delivery_id
            and dropped.rider_id == self._assigned_rider
        ):
            self._dropped = dropped

    @workflow.signal(name=OrderSagaSignal.FOOD_READY)
    async def food_ready(self) -> None:
        # From the restaurant, once, between confirmation and pickup. Recorded
        # in delivery-service as soon as the delivery exists (it's created
        # right after confirmation); run() waits for this before finishing.
        if (
            not self._confirmed
            or self._food_ready_signalled
            or self._picked_up
            or self._cancel is not None
        ):
            return
        self._food_ready_signalled = True
        await workflow.wait_condition(lambda: self._delivery_id is not None or self._finished)
        if self._delivery_id is None:
            return
        # The assigned rider's ETAs, recomputed now that the food is ready.
        eta: DeliveryEta | None = await self._delivery(
            DeliveryActivity.MARK_FOOD_READY, DeliveryRef(self._delivery_id), DeliveryEta | None
        )
        if eta is not None:
            self._pickup_eta = eta.pickup_eta
        self._food_ready_at = workflow.now()

    @workflow.signal(name=OrderSagaSignal.PICKED_UP)
    def picked_up(self) -> None:
        if self._cancel is None and self._assigned_rider is not None:
            self._picked_up = True

    @workflow.signal(name=OrderSagaSignal.DELIVERED)
    def delivered(self, actor: Actor | None = None) -> None:
        # From the rider's app (no actor), or a system_admin marking an
        # overdue order delivered.
        if self._picked_up and not self._delivered:
            self._delivered = True
            self._delivered_by = actor

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
        result_type: Any = None,
        retry_policy: RetryPolicy = ACTIVITY_RETRY_POLICY,
    ) -> Any:
        # Temporal hands the step to whichever service listens on `task_queue`
        # and waits for its result (retried on errors).
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
        # Runs in order-service: saves the new status and its event (sent on to
        # notifications and analytics through Kafka), and updates the live feeds.
        await self._activity(
            OrderActivity.TRANSITION_ORDER,
            TransitionOrderInput(
                order_id=self._order_id, to_status=status, actor=actor, reason=reason
            ),
            TaskQueue.ORDER_ACTIVITIES,
            TransitionOrderResult,
            retry_policy,
        )

    async def _delivery(
        self, name: str, arg: Any, result_type: Any = None, retry_policy: RetryPolicy = MUST_SUCCEED
    ) -> Any:
        return await self._activity(
            name, arg, TaskQueue.DELIVERY_ACTIVITIES, result_type, retry_policy
        )

    async def _release_rider(self, outcome: RiderReleaseOutcome) -> None:
        assert self._delivery_id is not None
        # Runs in delivery-service: frees the rider (back to available, or off
        # shift if they dropped out).
        await self._delivery(
            DeliveryActivity.RELEASE_RIDER,
            ReleaseRiderInput(delivery_id=self._delivery_id, outcome=outcome),
        )

    def _check_cancelled(self) -> None:
        if self._cancel is not None:
            raise _Cancelled(self._cancel)

    async def _wait_until(self, condition: Callable[[], bool], deadline: datetime) -> bool:
        """Waits for `condition` until `deadline`. False if the deadline came first."""
        remaining = deadline - workflow.now()
        if remaining <= timedelta(0):
            return condition()
        try:
            await workflow.wait_condition(condition, timeout=remaining)
        except TimeoutError:
            return False
        return True

    def _flag_overdue(self, stage: str, message: str) -> None:
        # For support: the saga step (Temporal UI), a log line, and a metric
        # to alert on.
        self._step = f"{stage}_overdue"
        workflow.logger.warning("Order %s: %s", self._order_id, message)
        workflow.metric_meter().create_counter(
            "order_saga_overdue", "Orders past a pickup or delivery deadline"
        ).add(1, {"stage": stage})

    async def _undo_and_finish(self, status: OrderStatus, reason: str, actor: Actor) -> str:
        self._step = "compensating"
        # Undo what was done so far, newest first (e.g. void the payment, give
        # the stock back), each in the service that did it.
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
        result = await self._run(data)
        # A food_ready handler may still be recording it.
        self._finished = True
        await workflow.wait_condition(workflow.all_handlers_finished)
        return result

    async def _run(self, data: OrderSagaInput) -> str:
        self._order_id = data.order_id
        try:
            prep_time_minutes = await self._place_order(data)
            await self._assign_and_pick_up(data, prep_time_minutes)
        except _Cancelled as cancelled:
            request = cancelled.request
            return await self._undo_and_finish(
                OrderStatus.CANCELLED, cancel_reason(request), request.actor
            )
        except _Failed as failed:
            return await self._undo_and_finish(failed.status, failed.reason, failed.actor)
        except ActivityError as error:
            # Retries exhausted, or an unexpected business error: rejected
            # before confirmation, cancelled after (the only way out of
            # `confirmed` / `assigned` besides pickup).
            status = OrderStatus.CANCELLED if self._confirmed else OrderStatus.REJECTED
            return await self._undo_and_finish(status, failure_reason(error), SYSTEM)
        return await self._deliver_and_settle(data)

    async def _place_order(self, data: OrderSagaInput) -> int:
        """Up to `confirmed`. Returns the restaurant's prep time (minutes)."""
        self._check_cancelled()

        # 1. Stock + capacity (atomic in restaurant-service).
        # Runs in restaurant-service: reserves the items and a kitchen slot.
        # Undone if a later step fails.
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
            # Runs in restaurant-service: uses up one redemption of the promo
            # code. Undone if a later step fails.
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
        # Worked out here; runs in order-service to save the discount, tax,
        # delivery fee and final total on the order.
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

        # 4. Payment authorization (captured on delivery).
        # Runs in payment-service: holds the total on the card (not charged yet).
        # Undone (hold released) if a later step fails.
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
        # The restaurant sees it on its live feed and gets an SMS.
        await self._transition(OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
        self._step = "awaiting_restaurant"
        # Waits for the restaurant's accept/reject (sent in by POST
        # …/restaurant-response), a cancel, or the timeout.
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
            raise _Failed(reason, actor=response.actor)

        # 7. Confirmed. The customer is notified.
        # Flagged before the transition: it can commit and still report
        # failure, and `cancelled` is valid from either status.
        self._confirmed = True
        await self._transition(OrderStatus.CONFIRMED, actor=response.actor)
        self._step = OrderStatus.CONFIRMED
        return reservation.prep_time_minutes

    async def _assign_and_pick_up(self, data: OrderSagaInput, prep_time_minutes: int) -> None:
        """Delivery, rider assignment (replacing riders who drop out), until
        the rider picks the food up."""
        # 8. Delivery record — the pickup is the restaurant, ready after prep.
        # Runs in delivery-service: creates the delivery. Cancelled if a later
        # step fails.
        assert data.pickup is not None, "orders without a pickup location can't be delivered"
        self._compensations.append(
            (DeliveryActivity.CANCEL_DELIVERY, TaskQueue.DELIVERY_ACTIVITIES)
        )
        ready_at = workflow.now() + timedelta(minutes=prep_time_minutes)
        created: CreateDeliveryResult = await self._delivery(
            DeliveryActivity.CREATE_DELIVERY,
            CreateDeliveryInput(
                order_id=data.order_id,
                customer_id=data.customer_id,
                pickup=data.pickup,
                dropoff=data.dropoff,
                ready_at=ready_at,
            ),
            CreateDeliveryResult,
            ACTIVITY_RETRY_POLICY,
        )
        if created.status == "cancelled":
            raise _Failed("delivery_cancelled", status=OrderStatus.CANCELLED)
        self._delivery_id = created.delivery_id

        # Riders who turned the delivery down (declined, let it expire,
        # dropped out) → when; kept across reassignments.
        turned_down: dict[str, datetime] = {}
        while True:
            # 9. Find a rider who accepts.
            rider_id = await self._find_rider(data, turned_down)
            self._assigned_rider = rider_id
            self._dropped = None

            # 10. Assigned: delivery, ETA, order (RiderAssigned).
            # Runs in delivery-service: marks the delivery assigned, then works
            # out the pickup and drop-off times (shown to the customer live).
            await self._delivery(
                DeliveryActivity.TRANSITION_DELIVERY,
                TransitionDeliveryInput(delivery_id=self._delivery_id, to_status="assigned"),
            )
            eta: DeliveryEta = await self._delivery(
                DeliveryActivity.COMPUTE_ETA, DeliveryRef(self._delivery_id), DeliveryEta
            )
            self._pickup_eta = eta.pickup_eta
            # A replacement rider leaves the order `assigned` (no-op).
            # The customer is told a rider is on the way.
            await self._transition(OrderStatus.ASSIGNED)

            # 11. Wait for pickup — or a cancel, or the rider dropping out.
            # (sent in by the rider's picked-up / drop-out endpoints, or a cancel)
            self._step = "awaiting_pickup"
            if workflow.patched(PICKUP_DEADLINES):
                rider_late = await self._await_pickup(data, ready_at)
            else:
                await workflow.wait_condition(self._pickup_wait_over)
                rider_late = False
            if self._picked_up:
                return
            self._check_cancelled()
            if rider_late:
                # Didn't come for the ready food: back in the pool, find
                # someone else.
                await self._release_rider(RiderReleaseOutcome.LATE)
            else:
                # Rider dropped out (D12): off shift, find someone else.
                await self._release_rider(RiderReleaseOutcome.DROPPED)
            turned_down[rider_id] = workflow.now()
            self._assigned_rider = None

    def _pickup_wait_over(self) -> bool:
        return self._picked_up or self._cancel is not None or self._dropped is not None

    async def _await_pickup(self, data: OrderSagaInput, ready_at: datetime) -> bool:
        """Waits for pickup, a cancel or a drop-out. True if instead the rider
        is late: the food is ready and they haven't come for it `pickup_grace`
        after the later of "ready" and their pickup ETA. Until the food is
        ready the kitchen is the one running late: flagged `food_overdue`
        after it was due (`ready_at`), the order is cancelled
        `kitchen_timeout` after it."""
        flag_at = ready_at + timedelta(seconds=data.food_overdue_seconds)
        give_up = ready_at + timedelta(seconds=data.kitchen_timeout_seconds)
        while (food_ready_at := self._food_ready_at) is None:
            deadline = give_up if self._food_flagged else min(flag_at, give_up)
            if await self._wait_until(
                lambda: self._pickup_wait_over() or self._food_ready_at is not None, deadline
            ):
                if self._pickup_wait_over():
                    return False
                self._step = "awaiting_pickup"  # ready: the rider's turn
            elif deadline == give_up:
                raise _Failed("kitchen_timeout", status=OrderStatus.CANCELLED)
            else:
                self._food_flagged = True
                self._flag_overdue("food", "the restaurant hasn't marked the food ready")
        pickup_eta = self._pickup_eta or food_ready_at
        late_at = max(food_ready_at, pickup_eta) + timedelta(seconds=data.pickup_grace_seconds)
        if await self._wait_until(self._pickup_wait_over, late_at):
            return False
        workflow.logger.info(
            "Order %s: rider %s hasn't picked up the ready food; replacing them",
            self._order_id,
            self._assigned_rider,
        )
        return True

    async def _find_rider(self, data: OrderSagaInput, turned_down: dict[str, datetime]) -> str:
        """Offers the delivery to the nearest free rider, one at a time, until
        one accepts. A rider who turned it down is asked again only after
        `rider_reoffer_after_seconds` — so a few riders nearby get another
        chance instead of the search running dry. Starts with riders close to
        the restaurant and looks farther out each time nobody is free
        (`rider_search_radii_km`). Gives up — cancelling the order — after
        `rider_search_timeout`."""
        assert self._delivery_id is not None
        deadline = workflow.now() + timedelta(seconds=data.rider_search_timeout_seconds)
        offer_timeout = timedelta(seconds=data.rider_offer_timeout_seconds)
        reoffer_after = timedelta(seconds=data.rider_reoffer_after_seconds)
        radius_step = 0
        while True:
            self._check_cancelled()
            now = workflow.now()
            if now >= deadline:
                raise _Failed("no_rider_available", status=OrderStatus.CANCELLED)

            self._step = "searching_rider"
            # Runs in delivery-service: offers the delivery to the nearest free
            # rider and holds them until they answer. None if nobody is free.
            offer: RiderOffer | None = await self._delivery(
                DeliveryActivity.OFFER_TO_NEAREST_RIDER,
                OfferRiderInput(
                    delivery_id=self._delivery_id,
                    exclude_rider_ids=[
                        rider for rider, at in turned_down.items() if now - at < reoffer_after
                    ],
                    offer_timeout_seconds=data.rider_offer_timeout_seconds,
                    radius_km=data.rider_search_radii_km[radius_step],
                ),
                RiderOffer | None,
                ACTIVITY_RETRY_POLICY,
            )
            if offer is None:
                # Nobody free: look again shortly, a step farther out (or stop
                # on cancel).
                radius_step = min(radius_step + 1, len(data.rider_search_radii_km) - 1)
                remaining = deadline - workflow.now()
                retry = min(timedelta(seconds=data.rider_search_retry_seconds), remaining)
                try:
                    await workflow.wait_condition(lambda: self._cancel is not None, timeout=retry)
                except TimeoutError:
                    pass
                continue

            self._offered_rider = offer.rider_id
            self._rider_response = None
            self._step = f"offered:{offer.rider_id}"
            # Waits for the rider's accept/decline (sent in by the rider's
            # endpoints), a cancel, or the offer expiring.
            try:
                await workflow.wait_condition(
                    lambda: self._rider_response is not None or self._cancel is not None,
                    timeout=offer_timeout,
                )
            except TimeoutError:
                self._offered_rider = None
                await self._release_rider(RiderReleaseOutcome.EXPIRED)
                turned_down[offer.rider_id] = workflow.now()
                continue
            self._offered_rider = None
            # A cancel arriving now is handled by cancel_delivery, which also
            # releases the rider holding the offer.
            self._check_cancelled()
            assert self._rider_response is not None
            if self._rider_response.accepted:
                return offer.rider_id
            await self._release_rider(RiderReleaseOutcome.DECLINED)
            turned_down[offer.rider_id] = workflow.now()

    async def _deliver_and_settle(self, data: OrderSagaInput) -> str:
        """After pickup: nothing is undone any more, every step must succeed."""
        assert self._delivery_id is not None
        self._compensations.clear()

        # 12. Picked up: the kitchen's capacity slot is freed.
        # Delivery-service and order-service record the pickup (the customer is
        # notified), then restaurant-service frees the kitchen slot.
        self._step = "picked_up"
        await self._delivery(
            DeliveryActivity.TRANSITION_DELIVERY,
            TransitionDeliveryInput(delivery_id=self._delivery_id, to_status="picked_up"),
        )
        await self._transition(OrderStatus.PICKED_UP, retry_policy=MUST_SUCCEED)
        await self._activity(
            RestaurantActivity.COMPLETE_RESERVATION,
            OrderRef(self._order_id),
            TaskQueue.RESTAURANT_ACTIVITIES,
            retry_policy=MUST_SUCCEED,
        )

        # 13. Wait for delivery (sent in by the rider's delivered endpoint, or
        # a system_admin's for an overdue order).
        self._step = "delivering"
        if workflow.patched(DELIVERY_OVERDUE):
            overdue_at = workflow.now() + timedelta(seconds=data.delivery_overdue_seconds)
            if not await self._wait_until(lambda: self._delivered, overdue_at):
                self._flag_overdue("delivery", "not delivered yet; a system_admin can mark it")
        await workflow.wait_condition(lambda: self._delivered)

        # 14. Delivered: final settlement (payment captured), rider freed.
        # Delivery-service records it, payment-service charges the held amount,
        # order-service marks the order delivered (customer and restaurant are
        # notified). Each is retried until it succeeds.
        await self._delivery(
            DeliveryActivity.TRANSITION_DELIVERY,
            TransitionDeliveryInput(delivery_id=self._delivery_id, to_status="delivered"),
        )
        await self._activity(
            PaymentActivity.CAPTURE,
            OrderRef(self._order_id),
            TaskQueue.PAYMENT_ACTIVITIES,
            PaymentResult,
            MUST_SUCCEED,
        )
        by_admin = self._delivered_by is not None
        await self._transition(
            OrderStatus.DELIVERED,
            actor=self._delivered_by or SYSTEM,
            reason="marked_delivered_by_admin" if by_admin else None,
            retry_policy=MUST_SUCCEED,
        )
        await self._release_rider(RiderReleaseOutcome.COMPLETED)
        self._step = OrderStatus.DELIVERED
        return OrderStatus.DELIVERED
