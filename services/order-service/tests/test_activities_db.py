"""order-activities against a real Postgres (week2-plan step 1)."""

from decimal import Decimal

import pytest
from app import activities, repository, tracking
from app.models import Order, OrderStatus, OrderStatusHistory, OutboxEvent
from sfo_contracts import BusinessErrorType
from sfo_contracts.activities import (
    Actor,
    ApplyPricingInput,
    RecordRefundInput,
    TransitionOrderInput,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from temporalio.exceptions import ApplicationError

from tests.db_helpers import OWNER, RESTAURANT, insert_order

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


async def _history(factory: Factory, order_id: str) -> list[tuple[str | None, str]]:
    async with factory() as session:
        rows = await session.execute(
            select(OrderStatusHistory.from_status, OrderStatusHistory.to_status)
            .where(OrderStatusHistory.order_id == order_id)
            .order_by(OrderStatusHistory.created_at)
        )
        return [tuple(r) for r in rows.all()]  # type: ignore[misc]


async def _events(factory: Factory, order_id: str) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            select(OutboxEvent.event_type)
            .where(OutboxEvent.aggregate_id == order_id)
            .order_by(OutboxEvent.created_at)
        )
        return list(rows.scalars().all())


async def _order(factory: Factory, order_id: str) -> Order:
    async with factory() as session:
        order = await repository.get_order(session, order_id)
        assert order is not None
        return order


def _to(order_id: str, status: OrderStatus, reason: str | None = None) -> TransitionOrderInput:
    return TransitionOrderInput(order_id=order_id, to_status=status, reason=reason)


async def test_transition_writes_history_and_event_but_publishes_nothing(
    session_factory: Factory, published: list
) -> None:
    order = await insert_order(session_factory)

    result = await activities.transition_order(
        _to(order.id, OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    )
    assert (result.status, result.changed) == (OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE, True)
    # The restaurant's "new order" event (week3-plan W3-D8a). The live feeds
    # get it from Kafka (tracking-service), not from a publish here.
    assert await _events(session_factory, order.id) == ["OrderAwaitingRestaurant"]
    assert published == []

    actor = Actor(id=OWNER, role="restaurant_admin")
    await activities.transition_order(
        TransitionOrderInput(order_id=order.id, to_status=OrderStatus.CONFIRMED, actor=actor)
    )
    stored = await _order(session_factory, order.id)
    assert stored.status == OrderStatus.CONFIRMED
    assert stored.confirmed_at is not None
    assert stored.version == 3
    assert await _events(session_factory, order.id) == [
        "OrderAwaitingRestaurant",
        "OrderConfirmed",
    ]
    # Enriched so consumers never call back (week3-plan W3-D8).
    async with session_factory() as session:
        payload = (
            await session.execute(
                select(OutboxEvent.payload).where(OutboxEvent.event_type == "OrderConfirmed")
            )
        ).scalar_one()
    assert payload["restaurant_admin_id"] == stored.restaurant_admin_id
    assert payload["total_amount"] == str(stored.total_amount)
    assert await _history(session_factory, order.id) == [
        (None, "created"),
        ("created", "awaiting_restaurant_acceptance"),
        ("awaiting_restaurant_acceptance", "confirmed"),
    ]
    assert stored.status_history[-1].actor_id == OWNER


async def test_transition_is_idempotent(session_factory: Factory, published: list) -> None:
    order = await insert_order(session_factory)
    first = await activities.transition_order(_to(order.id, OrderStatus.REJECTED, "out_of_stock"))
    again = await activities.transition_order(_to(order.id, OrderStatus.REJECTED, "out_of_stock"))
    assert (first.changed, again.changed) == (True, False)
    assert await _events(session_factory, order.id) == ["OrderRejected"]
    assert len(await _history(session_factory, order.id)) == 2
    stored = await _order(session_factory, order.id)
    assert (stored.status_reason, stored.rejected_at is not None) == ("out_of_stock", True)


async def test_disallowed_transition_is_a_non_retryable_error(
    session_factory: Factory, published: list
) -> None:
    order = await insert_order(session_factory, status=OrderStatus.PICKED_UP)
    with pytest.raises(ApplicationError) as exc_info:
        await activities.transition_order(_to(order.id, OrderStatus.CANCELLED))
    assert exc_info.value.type == BusinessErrorType.INVALID_ORDER_TRANSITION
    assert exc_info.value.non_retryable
    assert (await _order(session_factory, order.id)).status == OrderStatus.PICKED_UP


async def test_unknown_order_is_a_non_retryable_error(session_factory: Factory) -> None:
    with pytest.raises(ApplicationError) as exc_info:
        await activities.transition_order(_to("nope", OrderStatus.CONFIRMED))
    assert exc_info.value.type == BusinessErrorType.NOT_FOUND


async def test_concurrent_change_fails_on_version_and_writes_nothing(
    session_factory: Factory, published: list
) -> None:
    order = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    async with session_factory() as stale_session:
        stale = await repository.get_order(stale_session, order.id)
        assert stale is not None
        # Another writer gets there first.
        await activities.transition_order(_to(order.id, OrderStatus.CONFIRMED))

        repository.apply_transition(
            stale_session,
            stale,
            OrderStatus.CANCELLED,
            actor_id=None,
            actor_role=None,
            reason="customer_cancelled",
        )
        with pytest.raises(StaleDataError):
            await stale_session.commit()

    # The losing write left no history row and no event behind (same
    # transaction as the status change).
    assert (await _order(session_factory, order.id)).status == OrderStatus.CONFIRMED
    assert await _events(session_factory, order.id) == ["OrderConfirmed"]
    assert [to for _, to in await _history(session_factory, order.id)] == [
        "created",
        "confirmed",
    ]


async def test_apply_pricing_sets_total(session_factory: Factory) -> None:
    order = await insert_order(session_factory, subtotal=Decimal("1000.00"))
    pricing = ApplyPricingInput(
        order_id=order.id,
        discount=Decimal("100"),
        tax=Decimal("45.004"),
        delivery_fee=Decimal("160"),
    )
    result = await activities.apply_pricing(pricing)
    assert result.total_amount == Decimal("1105.00")
    assert (await activities.apply_pricing(pricing)).total_amount == Decimal("1105.00")
    stored = await _order(session_factory, order.id)
    assert (stored.discount_amount, stored.tax_amount, stored.delivery_fee) == (
        Decimal("100.00"),
        Decimal("45.00"),
        Decimal("160.00"),
    )


@pytest.mark.parametrize(
    "status,discount,error",
    [
        (OrderStatus.CONFIRMED, Decimal("0"), BusinessErrorType.INVALID_ORDER_TRANSITION),
        (OrderStatus.CREATED, Decimal("1000.01"), BusinessErrorType.PROMOTION_INVALID),
    ],
)
async def test_apply_pricing_rejects(
    session_factory: Factory, status: OrderStatus, discount: Decimal, error: BusinessErrorType
) -> None:
    order = await insert_order(session_factory, status=status)
    with pytest.raises(ApplicationError) as exc_info:
        await activities.apply_pricing(
            ApplyPricingInput(
                order_id=order.id, discount=discount, tax=Decimal("0"), delivery_fee=Decimal("0")
            )
        )
    assert exc_info.value.type == error


async def test_record_refund_once(session_factory: Factory) -> None:
    order = await insert_order(session_factory, status=OrderStatus.DELIVERED)
    refund = RecordRefundInput(
        order_id=order.id,
        amount=Decimal("250"),
        reason="cold food",
        actor=Actor(id="u-admin", role="system_admin"),
    )
    await activities.record_refund(refund)
    await activities.record_refund(refund)  # retried: no second refund

    stored = await _order(session_factory, order.id)
    assert stored.status == OrderStatus.DELIVERED
    assert stored.refunded_amount == Decimal("250.00")
    assert stored.refunded_at is not None
    async with session_factory() as session:
        refund_rows = await session.scalar(
            select(func.count())
            .select_from(OrderStatusHistory)
            .where(
                OrderStatusHistory.order_id == order.id,
                OrderStatusHistory.to_status == OrderStatus.DELIVERED,
            )
        )
    assert refund_rows == 1


@pytest.mark.parametrize(
    "status,amount,error",
    [
        (OrderStatus.CONFIRMED, Decimal("10"), BusinessErrorType.INVALID_ORDER_TRANSITION),
        (OrderStatus.DELIVERED, Decimal("1000.01"), BusinessErrorType.REFUND_EXCEEDS_CAPTURED),
        (OrderStatus.DELIVERED, Decimal("0"), BusinessErrorType.REFUND_EXCEEDS_CAPTURED),
    ],
)
async def test_record_refund_rejects(
    session_factory: Factory, status: OrderStatus, amount: Decimal, error: BusinessErrorType
) -> None:
    order = await insert_order(session_factory, status=status)
    with pytest.raises(ApplicationError) as exc_info:
        await activities.record_refund(
            RecordRefundInput(order_id=order.id, amount=amount, reason="x", actor=Actor())
        )
    assert exc_info.value.type == error


async def test_write_state_records_the_order_for_the_live_feeds(
    session_factory: Factory, tracking_redis
) -> None:
    """order-service writes the state only for a new order and when
    rebuilding a missing key; status changes reach the live feeds through
    their Kafka events (tracking-service's app/projection.py)."""
    order = await insert_order(session_factory, status=OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE)
    state_key = f"tracking:order:{order.id}:state"
    pending = f"tracking:restaurant:{RESTAURANT}:pending"

    await tracking.write_state(order)
    state = tracking_redis.hashes[state_key]
    assert (state["status"], state["customer_id"], state["restaurant_admin_id"]) == (
        "awaiting_restaurant_acceptance",
        "u-customer",
        OWNER,
    )
    assert tracking_redis.sets[pending] == {order.id}
    assert tracking_redis.ttls[state_key] == 24 * 3600

    await activities.transition_order(_to(order.id, OrderStatus.REJECTED, "restaurant_timeout"))
    # Untouched here: tracking-service applies the OrderRejected event.
    assert tracking_redis.hashes[state_key]["status"] == "awaiting_restaurant_acceptance"
    assert tracking_redis.published == []

    rejected = await insert_order(session_factory, status=OrderStatus.REJECTED)
    await tracking.write_state(rejected)
    assert tracking_redis.ttls[f"tracking:order:{rejected.id}:state"] == 3600  # finished: 1h
    assert order.id in tracking_redis.sets[pending]  # only the rejected one left it
