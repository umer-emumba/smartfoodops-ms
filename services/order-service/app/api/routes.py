import hashlib
from collections import Counter
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query, Response
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from sfo_common.middleware import get_correlation_id
from sfo_events import EventType, new_outbox_row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository, restaurant_client
from app.auth_deps import get_current_user, require_roles
from app.db import get_session
from app.models import (
    ORDER_STATUS_CREATED,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    OutboxEvent,
)
from app.schemas import OrderCreate, OrderResponse

router = APIRouter(prefix="/v1", tags=["orders"])

# Week 1: basic order creation only, no saga yet — see docs/PROGRESS.md.
# Week 2 adds the Temporal OrderSagaWorkflow that drives status transitions
# (capacity check, promo/tax/fee pricing, payment, restaurant acceptance).

_CENTS = Decimal("0.01")


def _request_hash(body: OrderCreate) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


def _replay_or_conflict(existing: Order, request_hash: str, response: Response) -> Order:
    if existing.request_hash != request_hash:
        raise ConflictError("Idempotency-Key was already used with a different request body")
    response.status_code = 200
    return existing


async def _build_items(body: OrderCreate) -> list[OrderItem]:
    """Validates the order against restaurant-service's live data and returns
    priced line items. Raises before anything is written, so a rejected order
    leaves no trace in order_db."""
    restaurant = await restaurant_client.get_restaurant(body.restaurant_id)
    if restaurant.status != "active":
        raise NotFoundError("Restaurant not found")
    if not restaurant.is_accepting_orders:
        raise ConflictError("Restaurant is not accepting orders right now")

    menu = await restaurant_client.get_menu(body.restaurant_id)
    # Summed per item, so the same item split across two lines can't slip
    # past the stock check.
    requested = Counter()
    for line in body.items:
        requested[line.menu_item_id] += line.quantity

    unknown = [item_id for item_id in requested if item_id not in menu]
    if unknown:
        raise ValidationAppError(
            "Some items are not on this restaurant's menu", details={"menu_item_ids": unknown}
        )
    unavailable = [
        item_id
        for item_id, qty in requested.items()
        if not menu[item_id].is_available
        or (menu[item_id].stock_count is not None and menu[item_id].stock_count < qty)
    ]
    if unavailable:
        raise ConflictError(
            "Some items are unavailable or out of stock", details={"menu_item_ids": unavailable}
        )

    return [
        OrderItem(
            menu_item_id=line.menu_item_id,
            item_name_snapshot=menu[line.menu_item_id].name,
            quantity=line.quantity,
            unit_price=menu[line.menu_item_id].price,
            special_instructions=line.special_instructions,
        )
        for line in body.items
    ]


@router.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=255),
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    session: AsyncSession = Depends(get_session),
) -> Order:
    """Requires an `Idempotency-Key` header. Retrying with the same key and
    body returns the original order (200) instead of creating a duplicate —
    safe for a client to retry after a timeout or a double-tap."""
    request_hash = _request_hash(body)
    existing = await repository.get_order_by_idempotency_key(session, current.id, idempotency_key)
    if existing is not None:
        return _replay_or_conflict(existing, request_hash, response)

    items = await _build_items(body)
    subtotal = sum((i.unit_price * i.quantity for i in items), Decimal("0")).quantize(_CENTS)
    # Tax/delivery-fee/discount computation (and promo_code validation) is
    # the saga's "price/discount" step starting Week 2 — Week 1 prices the
    # items server-side and records subtotal only.
    order = Order(
        customer_id=current.id,
        restaurant_id=body.restaurant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        delivery_address_text=body.delivery_address_text,
        delivery_lat=body.delivery_lat,
        delivery_lng=body.delivery_lng,
        special_instructions=body.special_instructions,
        promo_code=body.promo_code,
        subtotal=subtotal,
        total_amount=subtotal,
    )
    order.items = items
    order.status_history = [
        OrderStatusHistory(
            from_status=None,
            to_status=ORDER_STATUS_CREATED,
            actor_id=current.id,
            actor_role=current.role.value,
        )
    ]
    try:
        order = await repository.create_order(session, order)
        # Same transaction as the order insert — the transactional outbox.
        # Week 3's relay publishes undispatched rows to Kafka.
        session.add(
            new_outbox_row(
                OutboxEvent,
                aggregate_id=order.id,
                event_type=EventType.ORDER_PLACED,
                payload={
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "customer_id": order.customer_id,
                    "restaurant_id": order.restaurant_id,
                    "total_amount": str(order.total_amount),
                    "items": [
                        {"menu_item_id": i.menu_item_id, "quantity": i.quantity} for i in items
                    ],
                },
                correlation_id=get_correlation_id() or None,
            )
        )
        await session.commit()
    except IntegrityError:
        # Lost a race against a concurrent request with the same key.
        await session.rollback()
        existing = await repository.get_order_by_idempotency_key(
            session, current.id, idempotency_key
        )
        if existing is None:
            raise
        return _replay_or_conflict(existing, request_hash, response)
    return order


# Static path — must be registered before "/orders/{order_id}" below.
@router.get("/orders/admin", response_model=list[OrderResponse])
async def list_orders_admin(
    status: OrderStatus | None = Query(default=None),
    restaurant_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[Order]:
    """system_admin view across all customers and restaurants, newest first."""
    return await repository.list_orders_admin(
        session, status=status, restaurant_id=restaurant_id, customer_id=customer_id, limit=limit
    )


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Order:
    order = await repository.get_order(session, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    if order.customer_id != current.id and current.role != Role.SYSTEM_ADMIN:
        raise ForbiddenError("Not your order")
    return order


@router.get("/orders", response_model=list[OrderResponse])
async def list_my_orders(
    limit: int = Query(default=20, ge=1, le=100),
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    session: AsyncSession = Depends(get_session),
) -> list[Order]:
    return await repository.list_orders_for_customer(session, current.id, limit=limit)
