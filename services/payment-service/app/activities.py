"""payment-service's activities on `payment-activities` (docs/week2-plan.md
step 3): a simulated card — `card_simulated` approves,
`card_simulated_decline` declines, deterministically, so tests and the
lifecycle scenarios can exercise both paths.

Idempotency: one payment per order (`idempotency_key` = order_id), one
refund row per refund workflow ID. Each activity locks the payment row, so
concurrent calls for the same order serialize. A void that arrives before
any authorization (the saga compensating after an authorize attempt timed
out) leaves a `voided` placeholder, so a late authorize can't hold money
nobody will release."""

from datetime import UTC, datetime
from decimal import Decimal

from sfo_common.middleware import get_correlation_id
from sfo_contracts import BusinessErrorType, business_error
from sfo_contracts.activities import (
    AuthorizePaymentInput,
    OrderRef,
    PaymentActivity,
    PaymentResult,
    RefundPaymentInput,
    RefundPaymentResult,
)
from sfo_events import EventType, new_outbox_row
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app.db import SessionFactory
from app.models import OutboxEvent, Payment, Refund
from app.state_machine import PaymentStatus, can_transition

_CENTS = Decimal("0.01")
_DECLINING_METHOD = "card_simulated_decline"


def _result(payment: Payment) -> PaymentResult:
    return PaymentResult(payment_id=payment.id, status=payment.status)


def _declined(payment: Payment) -> Exception:
    return business_error(
        BusinessErrorType.PAYMENT_DECLINED, payment.failure_reason or "Payment declined"
    )


async def _locked_payment(session: AsyncSession, order_id: str) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.order_id == order_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def _require_payment(session: AsyncSession, order_id: str) -> Payment:
    payment = await _locked_payment(session, order_id)
    if payment is None:
        raise business_error(BusinessErrorType.NOT_FOUND, f"No payment for order {order_id}")
    return payment


def _invalid_state(payment: Payment, action: str) -> Exception:
    return business_error(
        BusinessErrorType.INVALID_PAYMENT_STATE,
        f"Can't {action} payment for order {payment.order_id}: it is {payment.status}",
    )


@activity.defn(name=PaymentActivity.AUTHORIZE)
async def authorize(data: AuthorizePaymentInput) -> PaymentResult:
    """Authorizes (holds) the order total. A decline is recorded and raised
    as PaymentDeclined (not retried); re-running a declined authorization
    raises it again. An existing payment for the order is returned as-is."""
    if data.amount <= 0:
        raise business_error(
            BusinessErrorType.INVALID_PAYMENT_STATE, f"Amount must be positive, got {data.amount}"
        )
    async with SessionFactory() as session:
        existing = await _locked_payment(session, data.order_id)
        if existing is None:
            now = datetime.now(UTC)
            declined = data.method == _DECLINING_METHOD
            payment = Payment(
                order_id=data.order_id,
                customer_id=data.customer_id,
                amount=data.amount.quantize(_CENTS),
                payment_method=data.method,
                idempotency_key=data.order_id,
                status=PaymentStatus.DECLINED if declined else PaymentStatus.AUTHORIZED,
                failure_reason="Card declined (simulated)" if declined else None,
                authorized_at=None if declined else now,
                created_at=now,
            )
            session.add(payment)
            try:
                # Flushed first so the event can carry the payment's ID; a
                # concurrent duplicate fails here.
                await session.flush()
                if not declined:
                    session.add(
                        new_outbox_row(
                            OutboxEvent,
                            aggregate_id=payment.id,
                            event_type=EventType.PAYMENT_SUCCESSFUL,
                            payload={
                                "payment_id": payment.id,
                                "order_id": payment.order_id,
                                "customer_id": payment.customer_id,
                                "amount": payment.amount,
                                "status": payment.status,
                            },
                            correlation_id=get_correlation_id() or None,
                        )
                    )
                await session.commit()
            except IntegrityError:
                # A concurrent attempt for the same order created it first.
                await session.rollback()
                existing = await _locked_payment(session, data.order_id)
                assert existing is not None
            else:
                existing = payment
        if existing.status == PaymentStatus.DECLINED:
            raise _declined(existing)
        return _result(existing)


@activity.defn(name=PaymentActivity.CAPTURE)
async def capture(data: OrderRef) -> PaymentResult:
    """Takes the authorized amount — on delivery (final settlement). No-op if
    already captured (or refunded since)."""
    async with SessionFactory() as session:
        payment = await _require_payment(session, data.order_id)
        if payment.status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED):
            return _result(payment)
        if not can_transition(payment.status, PaymentStatus.CAPTURED):
            raise _invalid_state(payment, "capture")
        payment.status = PaymentStatus.CAPTURED
        payment.captured_at = datetime.now(UTC)
        await session.commit()
        return _result(payment)


@activity.defn(name=PaymentActivity.VOID)
async def void(data: OrderRef) -> PaymentResult:
    """Compensation for authorize: releases the hold. No-op if already voided
    or declined (nothing held). A captured payment needs a refund instead."""
    async with SessionFactory() as session:
        payment = await _locked_payment(session, data.order_id)
        if payment is None:
            now = datetime.now(UTC)
            payment = Payment(
                order_id=data.order_id,
                customer_id="",
                amount=Decimal("0"),
                idempotency_key=data.order_id,
                status=PaymentStatus.VOIDED,
                failure_reason="Voided before authorization",
                voided_at=now,
                created_at=now,
            )
            session.add(payment)
            try:
                await session.commit()
            except IntegrityError:
                # authorize committed meanwhile — void that instead.
                await session.rollback()
                return await void(data)
            return _result(payment)
        if payment.status in (PaymentStatus.VOIDED, PaymentStatus.DECLINED):
            return _result(payment)
        if not can_transition(payment.status, PaymentStatus.VOIDED):
            raise _invalid_state(payment, "void")
        payment.status = PaymentStatus.VOIDED
        payment.voided_at = datetime.now(UTC)
        await session.commit()
        return _result(payment)


@activity.defn(name=PaymentActivity.REFUND)
async def refund(data: RefundPaymentInput) -> RefundPaymentResult:
    """Refunds a captured payment, in full (`amount=None`: whatever is left)
    or in part. The total refunded never exceeds the captured amount. Runs
    once per `refund_id`: a retry returns the original refund."""
    async with SessionFactory() as session:
        payment = await _require_payment(session, data.order_id)
        existing = await session.get(Refund, data.refund_id)
        if existing is not None:
            return RefundPaymentResult(
                payment_id=payment.id,
                refunded_amount=existing.amount,
                total_refunded=payment.refunded_amount,
            )
        if not can_transition(payment.status, PaymentStatus.REFUNDED):
            raise _invalid_state(payment, "refund")
        remaining = payment.amount - payment.refunded_amount
        amount = remaining if data.amount is None else data.amount.quantize(_CENTS)
        if amount <= 0 or amount > remaining:
            raise business_error(
                BusinessErrorType.REFUND_EXCEEDS_CAPTURED,
                f"Refund {amount} must be > 0 and <= {remaining} still refundable",
            )
        now = datetime.now(UTC)
        session.add(
            Refund(
                id=data.refund_id,
                payment_id=payment.id,
                amount=amount,
                reason=data.reason,
                created_at=now,
            )
        )
        payment.refunded_amount = payment.refunded_amount + amount
        payment.status = PaymentStatus.REFUNDED
        payment.refunded_at = now
        session.add(
            new_outbox_row(
                OutboxEvent,
                aggregate_id=payment.id,
                event_type=EventType.REFUND_PROCESSED,
                payload={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "customer_id": payment.customer_id,
                    "refund_id": data.refund_id,
                    "amount": amount,
                    "total_refunded": payment.refunded_amount,
                    "reason": data.reason,
                },
                correlation_id=get_correlation_id() or None,
            )
        )
        await session.commit()
        return RefundPaymentResult(
            payment_id=payment.id, refunded_amount=amount, total_refunded=payment.refunded_amount
        )


ACTIVITIES = [authorize, capture, void, refund]
