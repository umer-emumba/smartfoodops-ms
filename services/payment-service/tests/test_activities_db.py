"""payment-activities against a real Postgres (week2-plan step 3)."""

import asyncio
from decimal import Decimal

import pytest
from app import activities
from app.models import OutboxEvent, Payment, Refund
from sfo_contracts import BusinessErrorType
from sfo_contracts.activities import AuthorizePaymentInput, OrderRef, RefundPaymentInput
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


def _authorize(
    order_id: str = "o-1", amount: str = "1250.50", method: str = "card_simulated"
) -> AuthorizePaymentInput:
    return AuthorizePaymentInput(
        order_id=order_id,
        customer_id="u-customer",
        amount=Decimal(amount),
        method=method,  # type: ignore[arg-type]
    )


def _refund(refund_id: str, amount: str | None = None, order_id: str = "o-1") -> RefundPaymentInput:
    return RefundPaymentInput(
        order_id=order_id,
        refund_id=refund_id,
        reason="cold food",
        amount=None if amount is None else Decimal(amount),
    )


async def _payment(factory: Factory, order_id: str = "o-1") -> Payment:
    async with factory() as session:
        result = await session.execute(select(Payment).where(Payment.order_id == order_id))
        return result.scalar_one()


async def _count(factory: Factory, model: type, **where: str) -> int:
    async with factory() as session:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return (await session.execute(stmt)).scalar_one()


async def _business_error(call) -> ApplicationError:  # type: ignore[no-untyped-def]
    with pytest.raises(ApplicationError) as exc_info:
        await call
    assert exc_info.value.non_retryable
    return exc_info.value


async def test_authorize_is_idempotent_per_order(session_factory: Factory) -> None:
    first = await activities.authorize(_authorize())
    again = await activities.authorize(_authorize())  # retry
    assert first == again
    assert first.status == "authorized"
    payment = await _payment(session_factory)
    assert (payment.amount, payment.idempotency_key) == (Decimal("1250.50"), "o-1")
    assert await _count(session_factory, Payment) == 1
    assert await _count(session_factory, OutboxEvent, event_type="PaymentSuccessful") == 1


async def test_concurrent_authorizations_create_one_payment(session_factory: Factory) -> None:
    results = await asyncio.gather(*(activities.authorize(_authorize()) for _ in range(5)))
    assert len({r.payment_id for r in results}) == 1
    assert await _count(session_factory, Payment) == 1
    assert await _count(session_factory, OutboxEvent) == 1


async def test_decline_is_recorded_and_stays_declined(session_factory: Factory) -> None:
    declining = _authorize(method="card_simulated_decline")
    error = await _business_error(activities.authorize(declining))
    assert error.type == BusinessErrorType.PAYMENT_DECLINED
    # A retry can't turn a decline into an approval.
    error = await _business_error(activities.authorize(declining))
    assert error.type == BusinessErrorType.PAYMENT_DECLINED
    payment = await _payment(session_factory)
    assert (payment.status, payment.authorized_at) == ("declined", None)
    assert await _count(session_factory, OutboxEvent) == 0
    # Nothing was held, so voiding it is a no-op.
    assert (await activities.void(OrderRef("o-1"))).status == "declined"


async def test_capture_then_refund_in_parts(session_factory: Factory) -> None:
    await activities.authorize(_authorize(amount="1000"))
    assert (await activities.capture(OrderRef("o-1"))).status == "captured"
    assert (await activities.capture(OrderRef("o-1"))).status == "captured"  # retry

    first = await activities.refund(_refund("refund-a", "300"))
    assert (first.refunded_amount, first.total_refunded) == (Decimal("300.00"), Decimal("300.00"))
    again = await activities.refund(_refund("refund-a", "300"))  # retry
    assert again == first

    rest = await activities.refund(_refund("refund-b"))  # amount=None → the remainder
    assert (rest.refunded_amount, rest.total_refunded) == (Decimal("700.00"), Decimal("1000.00"))

    payment = await _payment(session_factory)
    assert (payment.status, payment.refunded_amount) == ("refunded", Decimal("1000.00"))
    assert payment.captured_at is not None and payment.refunded_at is not None
    assert await _count(session_factory, Refund) == 2
    assert await _count(session_factory, OutboxEvent, event_type="RefundProcessed") == 2


async def test_refund_never_exceeds_captured(session_factory: Factory) -> None:
    await activities.authorize(_authorize(amount="1000"))
    await activities.capture(OrderRef("o-1"))
    error = await _business_error(activities.refund(_refund("refund-a", "1000.01")))
    assert error.type == BusinessErrorType.REFUND_EXCEEDS_CAPTURED
    await activities.refund(_refund("refund-a", "600"))
    error = await _business_error(activities.refund(_refund("refund-b", "400.01")))
    assert error.type == BusinessErrorType.REFUND_EXCEEDS_CAPTURED
    assert (await _payment(session_factory)).refunded_amount == Decimal("600.00")


async def test_concurrent_refunds_never_exceed_captured(session_factory: Factory) -> None:
    await activities.authorize(_authorize(amount="1000"))
    await activities.capture(OrderRef("o-1"))
    results = await asyncio.gather(
        *(activities.refund(_refund(f"refund-{n}", "300")) for n in range(5)),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    assert len(succeeded) == 3  # 3 x 300 fits in 1000; a 4th doesn't
    assert all(
        isinstance(r, ApplicationError) and r.type == BusinessErrorType.REFUND_EXCEEDS_CAPTURED
        for r in results
        if isinstance(r, BaseException)
    )
    assert (await _payment(session_factory)).refunded_amount == Decimal("900.00")


async def test_void_releases_the_hold_once(session_factory: Factory) -> None:
    await activities.authorize(_authorize())
    assert (await activities.void(OrderRef("o-1"))).status == "voided"
    assert (await activities.void(OrderRef("o-1"))).status == "voided"  # retry
    assert (await _payment(session_factory)).voided_at is not None


async def test_capture_after_void_is_rejected(session_factory: Factory) -> None:
    await activities.authorize(_authorize())
    await activities.void(OrderRef("o-1"))
    error = await _business_error(activities.capture(OrderRef("o-1")))
    assert error.type == BusinessErrorType.INVALID_PAYMENT_STATE


@pytest.mark.parametrize("captured", [False, True])
async def test_refund_only_after_capture_and_void_only_before(
    session_factory: Factory, captured: bool
) -> None:
    await activities.authorize(_authorize())
    if captured:
        await activities.capture(OrderRef("o-1"))
        error = await _business_error(activities.void(OrderRef("o-1")))
    else:
        error = await _business_error(activities.refund(_refund("refund-a")))
    assert error.type == BusinessErrorType.INVALID_PAYMENT_STATE


async def test_void_before_authorize_blocks_a_late_authorize(session_factory: Factory) -> None:
    await activities.void(OrderRef("o-1"))
    result = await activities.authorize(_authorize())
    assert result.status == "voided"
    assert await _count(session_factory, OutboxEvent) == 0


async def test_capture_without_payment_is_not_found(session_factory: Factory) -> None:
    error = await _business_error(activities.capture(OrderRef("o-1")))
    assert error.type == BusinessErrorType.NOT_FOUND


async def test_non_positive_amount_is_rejected(session_factory: Factory) -> None:
    error = await _business_error(activities.authorize(_authorize(amount="0")))
    assert error.type == BusinessErrorType.INVALID_PAYMENT_STATE
