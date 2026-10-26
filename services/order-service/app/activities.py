"""order-service's activities on `order-activities` (docs/week2-plan.md
step 1). Each one is a single short transaction and safe to re-run: Temporal
retries an activity whose outcome it didn't see (worker crash, timeout), so
a re-run must not apply the change twice."""

from datetime import UTC, datetime
from decimal import Decimal

from sfo_contracts import BusinessErrorType, business_error
from sfo_contracts.activities import (
    ApplyPricingInput,
    ApplyPricingResult,
    OrderActivity,
    RecordRefundInput,
    TransitionOrderInput,
    TransitionOrderResult,
)
from sfo_events.reasons import reason_label
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app import repository
from app.db import SessionFactory
from app.metrics import SAGA_OUTCOMES
from app.models import Order, OrderStatus, OrderStatusHistory
from app.state_machine import TERMINAL, can_transition

_CENTS = Decimal("0.01")


async def _load(session: AsyncSession, order_id: str) -> Order:
    order = await repository.get_order(session, order_id)
    if order is None:
        raise business_error(BusinessErrorType.NOT_FOUND, f"Order {order_id} not found")
    return order


@activity.defn(name=OrderActivity.TRANSITION_ORDER)
async def transition_order(data: TransitionOrderInput) -> TransitionOrderResult:
    """Status change + history row + outbox event in one transaction. The
    event also drives the live feeds (tracking-service consumes it). Already
    in `to_status` → no-op success (a retry after a commit whose result was
    lost). A concurrent change fails the commit on the `version` check; that
    error is retried, and the retry re-reads the order and re-checks the
    transition."""
    to_status = OrderStatus(data.to_status)
    async with SessionFactory() as session:
        order = await _load(session, data.order_id)
        if order.status == to_status:
            return TransitionOrderResult(status=order.status, changed=False)
        if not can_transition(order.status, to_status):
            raise business_error(
                BusinessErrorType.INVALID_ORDER_TRANSITION,
                f"Order {order.id} can't go from {order.status} to {to_status}",
            )
        repository.apply_transition(
            session,
            order,
            to_status,
            actor_id=data.actor.id,
            actor_role=data.actor.role,
            reason=data.reason,
        )
        await session.commit()
        if to_status in TERMINAL:
            SAGA_OUTCOMES.labels(to_status.value, reason_label(data.reason)).inc()
        return TransitionOrderResult(status=order.status, changed=True)


@activity.defn(name=OrderActivity.APPLY_PRICING)
async def apply_pricing(data: ApplyPricingInput) -> ApplyPricingResult:
    """Writes the saga's pricing (discount, tax, delivery fee) and the
    resulting total. Only while the order is `created` — pricing happens
    before payment is authorized for that total. Re-running writes the same
    values."""
    async with SessionFactory() as session:
        order = await _load(session, data.order_id)
        if order.status != OrderStatus.CREATED:
            raise business_error(
                BusinessErrorType.INVALID_ORDER_TRANSITION,
                f"Order {order.id} is {order.status}; pricing is only set while created",
            )
        if data.discount > order.subtotal:
            raise business_error(
                BusinessErrorType.PROMOTION_INVALID,
                f"Discount {data.discount} exceeds subtotal {order.subtotal}",
            )
        order.discount_amount = data.discount.quantize(_CENTS)
        order.tax_amount = data.tax.quantize(_CENTS)
        order.delivery_fee = data.delivery_fee.quantize(_CENTS)
        order.total_amount = (
            order.subtotal - order.discount_amount + order.tax_amount + order.delivery_fee
        )
        await session.commit()
        return ApplyPricingResult(total_amount=order.total_amount)


@activity.defn(name=OrderActivity.RECORD_REFUND)
async def record_refund(data: RecordRefundInput) -> None:
    """Records a post-delivery refund (week2-plan D13): refunded amount,
    time, and a delivered → delivered history row. There is one refund
    workflow per order (plan §9), so an order that already has `refunded_at`
    is a re-run — no-op."""
    async with SessionFactory() as session:
        order = await _load(session, data.order_id)
        if order.refunded_at is not None:
            return
        if order.status != OrderStatus.DELIVERED:
            raise business_error(
                BusinessErrorType.INVALID_ORDER_TRANSITION,
                f"Order {order.id} is {order.status}; only delivered orders are refunded",
            )
        if data.amount <= 0 or data.amount > order.total_amount:
            raise business_error(
                BusinessErrorType.REFUND_EXCEEDS_CAPTURED,
                f"Refund {data.amount} must be > 0 and <= order total {order.total_amount}",
            )
        order.refunded_amount = data.amount.quantize(_CENTS)
        order.refunded_at = datetime.now(UTC)
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=order.status,
                to_status=order.status,
                actor_id=data.actor.id,
                actor_role=data.actor.role,
                reason=f"refunded {order.refunded_amount}: {data.reason}",
            )
        )
        await session.commit()


ACTIVITIES = [transition_order, apply_pricing, record_refund]
