from datetime import UTC, datetime

from sfo_common.pagination import build_page, paginate_newest_first
from sfo_events import EventType, new_outbox_row
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Order, OrderStatus, OrderStatusHistory, OutboxEvent
from app.state_machine import ENTERED_AT_COLUMN

# The domain event each status emits (Week 3 publishes them to Kafka).
# `awaiting_restaurant_acceptance` is an internal step — no event.
_TRANSITION_EVENTS: dict[OrderStatus, EventType] = {
    OrderStatus.CONFIRMED: EventType.ORDER_CONFIRMED,
    OrderStatus.REJECTED: EventType.ORDER_REJECTED,
    OrderStatus.CANCELLED: EventType.ORDER_CANCELLED,
    OrderStatus.ASSIGNED: EventType.RIDER_ASSIGNED,
    OrderStatus.PICKED_UP: EventType.ORDER_PICKED_UP,
    OrderStatus.DELIVERED: EventType.ORDER_DELIVERED,
}


async def create_order(session: AsyncSession, order: Order) -> Order:
    session.add(order)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: str) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    )
    return result.scalar_one_or_none()


async def get_order_by_idempotency_key(
    session: AsyncSession, customer_id: str, idempotency_key: str
) -> Order | None:
    result = await session.execute(
        select(Order)
        .where(Order.customer_id == customer_id, Order.idempotency_key == idempotency_key)
        .options(selectinload(Order.items))
    )
    return result.scalar_one_or_none()


async def list_orders_admin(
    session: AsyncSession,
    *,
    status: str | None,
    restaurant_id: str | None,
    customer_id: str | None,
    cursor: str | None,
    limit: int,
) -> dict:
    stmt = select(Order).options(selectinload(Order.items))
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if restaurant_id is not None:
        stmt = stmt.where(Order.restaurant_id == restaurant_id)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    result = await session.execute(paginate_newest_first(stmt, Order, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)


async def list_orders_for_customer(
    session: AsyncSession, customer_id: str, *, cursor: str | None, limit: int
) -> dict:
    stmt = select(Order).where(Order.customer_id == customer_id).options(selectinload(Order.items))
    result = await session.execute(paginate_newest_first(stmt, Order, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)


async def list_orders_for_restaurant(
    session: AsyncSession,
    restaurant_id: str,
    *,
    admin_id: str | None,
    status: str | None,
    cursor: str | None,
    limit: int,
) -> dict:
    """`admin_id` set → only orders owned by that restaurant admin (the
    snapshotted owner, week2-plan D10); None → all (system_admin)."""
    stmt = select(Order).where(Order.restaurant_id == restaurant_id)
    if admin_id is not None:
        stmt = stmt.where(Order.restaurant_admin_id == admin_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.options(selectinload(Order.items))
    result = await session.execute(paginate_newest_first(stmt, Order, cursor=cursor, limit=limit))
    return build_page(list(result.scalars().all()), limit)


async def get_restaurant_owner(session: AsyncSession, restaurant_id: str) -> str | None:
    """The owner snapshotted on the restaurant's most recent order, or None
    if it has no orders (yet)."""
    result = await session.execute(
        select(Order.restaurant_admin_id)
        .where(Order.restaurant_id == restaurant_id, Order.restaurant_admin_id.is_not(None))
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def apply_transition(
    session: AsyncSession,
    order: Order,
    to_status: OrderStatus,
    *,
    actor_id: str | None,
    actor_role: str | None,
    reason: str | None,
) -> None:
    """Moves `order` to `to_status` and adds the history row and outbox event
    to the session — the caller commits all three together. The caller has
    already checked the transition is allowed (app/state_machine.py). A
    concurrent change to the same order fails the commit with StaleDataError
    (the `version` column)."""
    from_status = order.status
    now = datetime.now(UTC)
    order.status = to_status
    if to_status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
        order.status_reason = reason
    column = ENTERED_AT_COLUMN.get(to_status)
    if column is not None:
        setattr(order, column, now)
    session.add(
        OrderStatusHistory(
            order_id=order.id,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
            created_at=now,
        )
    )
    event_type = _TRANSITION_EVENTS.get(to_status)
    if event_type is not None:
        session.add(
            new_outbox_row(
                OutboxEvent,
                aggregate_id=order.id,
                event_type=event_type,
                payload={
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "customer_id": order.customer_id,
                    "restaurant_id": order.restaurant_id,
                    "from_status": from_status,
                    "to_status": to_status.value,
                    "reason": reason,
                },
            )
        )
