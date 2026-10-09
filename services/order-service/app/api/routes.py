import hashlib
from collections import Counter
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query, Response
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from sfo_common.middleware import get_correlation_id
from sfo_common.pagination import Page
from sfo_contracts import OrderSagaSignal
from sfo_contracts.activities import Actor
from sfo_contracts.workflows import CancelRequest, RestaurantResponse
from sfo_events import EventType, new_outbox_row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository, restaurant_client, temporal_client
from app.auth_deps import get_current_user, require_roles
from app.db import get_session
from app.models import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    OutboxEvent,
)
from app.schemas import (
    CancelOrderRequest,
    OrderCreate,
    OrderResponse,
    RestaurantResponseRequest,
)
from app.state_machine import can_cancel

router = APIRouter(prefix="/v1", tags=["orders"])

# Status transitions are made only by the Temporal order saga (its
# transition_order activity, app/activities.py). Endpoints that change an
# order's course — restaurant response, cancel — check the current status
# for a fast 409, then signal the saga.

_CENTS = Decimal("0.01")


def _request_hash(body: OrderCreate) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


def _replay_or_conflict(existing: Order, request_hash: str, response: Response) -> Order:
    if existing.request_hash != request_hash:
        raise ConflictError("Idempotency-Key was already used with a different request body")
    response.status_code = 200
    return existing


def _can_supply(item: restaurant_client.MenuItemSnapshot, quantity: int) -> bool:
    """Available, and either unlimited stock (None) or enough of it."""
    return item.is_available and (item.stock_count is None or item.stock_count >= quantity)


async def _build_items(
    body: OrderCreate,
) -> tuple[restaurant_client.RestaurantSnapshot, list[OrderItem]]:
    """Validates the order against restaurant-service's live data and returns
    priced line items. Raises before anything is written, so a rejected order
    leaves no trace in order_db."""
    # Summed per item, so the same item split across two lines can't slip
    # past the stock check.
    requested: Counter[str] = Counter()
    for line in body.items:
        requested[line.menu_item_id] += line.quantity

    restaurant, menu = await restaurant_client.get_quote(body.restaurant_id, list(requested))
    # Same rule the public endpoints apply: a pending/suspended restaurant
    # doesn't exist as far as customers are concerned.
    if restaurant.status != "active":
        raise NotFoundError("Restaurant not found")
    if not restaurant.is_accepting_orders:
        raise ConflictError("Restaurant is not accepting orders right now")

    unknown = [item_id for item_id in requested if item_id not in menu]
    if unknown:
        raise ValidationAppError(
            "Some items are not on this restaurant's menu", details={"menu_item_ids": unknown}
        )
    unavailable = [
        item_id for item_id, qty in requested.items() if not _can_supply(menu[item_id], qty)
    ]
    if unavailable:
        raise ConflictError(
            "Some items are unavailable or out of stock", details={"menu_item_ids": unavailable}
        )

    return restaurant, [
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
    # End the read transaction before the network call below, so this request
    # doesn't hold a pooled DB connection idle while waiting on
    # restaurant-service. The session starts a fresh transaction for the write.
    await session.rollback()

    restaurant, items = await _build_items(body)
    subtotal = sum((i.unit_price * i.quantity for i in items), Decimal("0")).quantize(_CENTS)
    # Tax/delivery-fee/discount computation (and promo_code validation) is
    # the saga's "price/discount" step starting Week 2 — Week 1 prices the
    # items server-side and records subtotal only.
    order = Order(
        customer_id=current.id,
        restaurant_id=body.restaurant_id,
        restaurant_admin_id=restaurant.admin_user_id,
        restaurant_lat=restaurant.lat,
        restaurant_lng=restaurant.lng,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        delivery_address_text=body.delivery_address_text,
        delivery_lat=body.delivery_lat,
        delivery_lng=body.delivery_lng,
        special_instructions=body.special_instructions,
        promo_code=body.promo_code,
        payment_method=body.payment_method,
        subtotal=subtotal,
        total_amount=subtotal,
    )
    order.items = items
    order.status_history = [
        OrderStatusHistory(
            from_status=None,
            to_status=OrderStatus.CREATED,
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
@router.get("/orders/admin", response_model=Page[OrderResponse])
async def list_orders_admin(
    status: OrderStatus | None = Query(default=None),
    restaurant_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """system_admin view across all customers and restaurants, newest first.
    Pass the previous page's `next_cursor` to get the next page."""
    return await repository.list_orders_admin(
        session,
        status=status,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        cursor=cursor,
        limit=limit,
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
    # The ordering customer, the restaurant's owner, or a system_admin.
    if current.id not in (order.customer_id, order.restaurant_admin_id) and (
        current.role != Role.SYSTEM_ADMIN
    ):
        raise ForbiddenError("Not your order")
    return order


@router.get("/orders", response_model=Page[OrderResponse])
async def list_my_orders(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await repository.list_orders_for_customer(
        session, current.id, cursor=cursor, limit=limit
    )


@router.get("/orders/restaurant/{restaurant_id}", response_model=Page[OrderResponse])
async def list_restaurant_orders(
    restaurant_id: str,
    status: OrderStatus | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A restaurant's order queue, newest first — e.g. `?status=awaiting_restaurant_acceptance`
    for orders waiting on accept/reject. Owning restaurant_admin or system_admin."""
    admin_id = None
    if current.role == Role.RESTAURANT_ADMIN:
        # Ownership from the owner snapshotted on the restaurant's orders
        # (week2-plan D10) — no call to restaurant-service. A restaurant with
        # no orders yet has nothing to show anyway.
        owner = await repository.get_restaurant_owner(session, restaurant_id)
        if owner is not None and owner != current.id:
            raise ForbiddenError("Not your restaurant")
        admin_id = current.id
    return await repository.list_orders_for_restaurant(
        session, restaurant_id, admin_id=admin_id, status=status, cursor=cursor, limit=limit
    )


async def _get_order_or_404(session: AsyncSession, order_id: str) -> Order:
    order = await repository.get_order(session, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    return order


async def _signal_and_respond(
    session: AsyncSession, order: Order, signal: OrderSagaSignal, payload: object
) -> OrderResponse:
    """Responds with the order as it is now (202 — the saga applies the
    change asynchronously; GET /v1/orders/{id} or live tracking shows it)."""
    snapshot = OrderResponse.model_validate(order)
    # Release the DB connection before the network call. This expires
    # `order`, so only `snapshot` is used from here on.
    await session.rollback()
    await temporal_client.signal_order_saga(snapshot.id, signal, payload)
    return snapshot


@router.post(
    "/orders/{order_id}/restaurant-response", response_model=OrderResponse, status_code=202
)
async def restaurant_response(
    order_id: str,
    body: RestaurantResponseRequest,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> OrderResponse:
    """The owning restaurant accepts or rejects an order that is
    `awaiting_restaurant_acceptance`. Rejecting releases stock and voids
    the payment (saga compensation)."""
    order = await _get_order_or_404(session, order_id)
    if order.restaurant_admin_id != current.id:
        raise ForbiddenError("Not your restaurant's order")
    if order.status != OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE:
        raise ConflictError(f"Order is {order.status}, not awaiting restaurant acceptance")
    return await _signal_and_respond(
        session,
        order,
        OrderSagaSignal.RESTAURANT_RESPONSE,
        RestaurantResponse(decision=body.decision, reason=body.reason),
    )


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse, status_code=202)
async def cancel_order(
    order_id: str,
    body: CancelOrderRequest,
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> OrderResponse:
    """The ordering customer (or a system_admin) cancels, up until pickup.
    The saga compensates for whatever has happened so far."""
    order = await _get_order_or_404(session, order_id)
    if current.role == Role.CUSTOMER and order.customer_id != current.id:
        raise ForbiddenError("Not your order")
    if not can_cancel(order.status):
        raise ConflictError(f"Order is {order.status} and can no longer be cancelled")
    return await _signal_and_respond(
        session,
        order,
        OrderSagaSignal.CANCEL,
        CancelRequest(actor=Actor(id=current.id, role=current.role.value), reason=body.reason),
    )
