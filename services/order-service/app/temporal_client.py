"""Starting and signalling the order saga (docs/week2-plan.md §2, D4).
The API uses a lazily connected client (`get_client`); the reconciler
passes the worker's own."""

from sfo_common import temporal
from sfo_common.temporal import lazy_client
from sfo_contracts import OrderSagaSignal, TaskQueue, WorkflowName, order_workflow_id
from sfo_contracts.activities import GeoPoint, ReservedItem
from sfo_contracts.workflows import OrderSagaInput, PricingConfig
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.config import settings
from app.models import Order

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await lazy_client(settings)
    return _client


async def signal_order_saga(order_id: str, signal: OrderSagaSignal, payload: object) -> None:
    """Signals workflow `order-{order_id}` (sfo_common.temporal.signal_order_saga)."""
    await temporal.signal_order_saga(await get_client(), order_id, signal, payload)


def saga_input(order: Order) -> OrderSagaInput:
    """Everything the saga needs, from the order row (items loaded). The
    pricing config and timeout are today's settings, fixed for this order."""
    pickup = None
    if order.restaurant_lat is not None and order.restaurant_lng is not None:
        pickup = GeoPoint(lat=order.restaurant_lat, lng=order.restaurant_lng)
    return OrderSagaInput(
        order_id=order.id,
        customer_id=order.customer_id,
        restaurant_id=order.restaurant_id,
        items=[ReservedItem(menu_item_id=i.menu_item_id, quantity=i.quantity) for i in order.items],
        subtotal=order.subtotal,
        payment_method=order.payment_method,  # type: ignore[arg-type]
        pickup=pickup,
        dropoff=GeoPoint(lat=order.delivery_lat, lng=order.delivery_lng),
        pricing=PricingConfig(
            tax_rate=settings.tax_rate,
            base_delivery_fee=settings.base_delivery_fee,
            per_km_fee=settings.per_km_fee,
        ),
        restaurant_accept_timeout_seconds=settings.restaurant_accept_timeout_seconds,
        promo_code=order.promo_code,
    )


async def start_order_saga(client: Client, order: Order) -> bool:
    """Starts workflow `order-{id}`. Returns False if it's already running
    (or already finished): one saga per order (D4). A run that *failed* (a
    bug, not a business outcome) may be started again."""
    try:
        await client.start_workflow(
            WorkflowName.ORDER_SAGA,
            saga_input(order),
            id=order_workflow_id(order.id),
            task_queue=TaskQueue.ORDER_SAGA,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        return False
    return True
