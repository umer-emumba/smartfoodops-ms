"""The API's Temporal client: endpoints signal the order saga through it
(docs/week2-plan.md §2 — restaurant response, customer cancel)."""

from sfo_common.errors import ConflictError, ServiceUnavailableError
from sfo_common.temporal import lazy_client
from sfo_contracts import OrderSagaSignal, order_workflow_id
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from app.config import settings

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await lazy_client(settings)
    return _client


async def signal_order_saga(order_id: str, signal: OrderSagaSignal, payload: object) -> None:
    """Signals workflow `order-{order_id}`. The caller has already checked the
    order's status; the workflow re-checks it, since the order can move on
    between that check and the signal arriving."""
    client = await get_client()
    try:
        await client.get_workflow_handle(order_workflow_id(order_id)).signal(signal, payload)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            # Workflow finished, or (before the reconciler restarts it) never started.
            raise ConflictError("This order is not being processed right now") from exc
        raise ServiceUnavailableError("Order workflow service is unavailable") from exc
