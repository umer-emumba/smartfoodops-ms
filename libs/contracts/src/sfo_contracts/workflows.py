"""Workflow, signal and query names, workflow IDs, and signal payloads
(docs/week2-plan.md §5). REST endpoints in order-service and delivery-service
signal the order saga with these."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sfo_contracts.activities import Actor


class WorkflowName(StrEnum):
    ORDER_SAGA = "OrderSagaWorkflow"
    ORDER_REFUND = "OrderRefundWorkflow"
    # Diagnostic: pings every service's worker (order-service app/workflows/ping.py).
    WORKER_PING = "WorkerPingWorkflow"


class OrderSagaSignal(StrEnum):
    RESTAURANT_RESPONSE = "restaurant_response"
    RIDER_RESPONSE = "rider_response"
    RIDER_DROPPED = "rider_dropped"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCEL = "cancel"


class OrderSagaQuery(StrEnum):
    STATUS = "status"


def order_workflow_id(order_id: str) -> str:
    """One saga per order: starting it twice is a no-op (D4)."""
    return f"order-{order_id}"


def refund_workflow_id(order_id: str) -> str:
    """One refund workflow per order (D13, plan §9)."""
    return f"refund-{order_id}"


# ---------- Signal payloads ----------


@dataclass(frozen=True)
class RestaurantResponse:
    decision: Literal["accept", "reject"]
    reason: str | None = None


@dataclass(frozen=True)
class RiderResponse:
    delivery_id: str
    accepted: bool


@dataclass(frozen=True)
class RiderDropped:
    delivery_id: str
    reason: str


@dataclass(frozen=True)
class CancelRequest:
    actor: Actor
    reason: str | None = None


# ---------- Workflow inputs ----------


@dataclass(frozen=True)
class OrderRefundInput:
    order_id: str
    amount: Decimal
    reason: str
    actor: Actor
