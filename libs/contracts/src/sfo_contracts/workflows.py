"""Workflow, signal and query names, workflow IDs, and signal payloads
(docs/week2-plan.md §5). REST endpoints in order-service and delivery-service
signal the order saga with these."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sfo_contracts.activities import SYSTEM, Actor, GeoPoint, PaymentMethod, ReservedItem


class WorkflowName(StrEnum):
    ORDER_SAGA = "OrderSagaWorkflow"
    ORDER_REFUND = "OrderRefundWorkflow"
    # Diagnostic: pings every service's worker (order-service app/workflows/ping.py).
    WORKER_PING = "WorkerPingWorkflow"


class OrderSagaSignal(StrEnum):
    RESTAURANT_RESPONSE = "restaurant_response"
    RIDER_RESPONSE = "rider_response"
    RIDER_DROPPED = "rider_dropped"
    FOOD_READY = "food_ready"
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
    actor: Actor = SYSTEM


@dataclass(frozen=True)
class RiderResponse:
    delivery_id: str
    # Which rider answered: a late answer from a rider whose offer already
    # expired must not count as the current rider's.
    rider_id: str
    accepted: bool


@dataclass(frozen=True)
class RiderDropped:
    delivery_id: str
    rider_id: str
    reason: str


@dataclass(frozen=True)
class CancelRequest:
    actor: Actor
    reason: str | None = None


# ---------- Workflow inputs ----------


@dataclass(frozen=True)
class PricingConfig:
    """Captured when the saga starts, so a settings change doesn't alter an
    order already in flight (and replays stay deterministic)."""

    tax_rate: Decimal
    base_delivery_fee: Decimal
    per_km_fee: Decimal


@dataclass(frozen=True)
class OrderSagaInput:
    order_id: str
    customer_id: str
    restaurant_id: str
    items: list[ReservedItem]
    subtotal: Decimal
    payment_method: PaymentMethod
    pickup: GeoPoint | None  # None for orders created before it was recorded
    dropoff: GeoPoint
    pricing: PricingConfig
    restaurant_accept_timeout_seconds: int
    promo_code: str | None = None
    # Rider assignment (docs/week2-plan.md §7).
    rider_offer_timeout_seconds: int = 60
    rider_search_timeout_seconds: int = 600
    # How often to look again while no rider is free.
    rider_search_retry_seconds: int = 30
    # A rider who declined or let the offer expire is asked again after this.
    rider_reoffer_after_seconds: int = 120
    # Search radius around the restaurant: starts at the first and widens
    # one step each time nobody is free, staying at the last.
    rider_search_radii_km: list[float] = field(default_factory=lambda: [5.0, 8.0, 12.0])
    # Kitchen deadlines, from when the food was due (confirmation + prep
    # time): not marked ready this long after → flagged; this long → the
    # order is cancelled (`kitchen_timeout`).
    food_overdue_seconds: int = 900
    kitchen_timeout_seconds: int = 3600
    # Food ready but not picked up this long after the later of "ready" and
    # the rider's pickup ETA → the rider is replaced.
    pickup_grace_seconds: int = 900
    # Not delivered this long after pickup: flagged for support (who can mark
    # it delivered); the saga keeps waiting.
    delivery_overdue_seconds: int = 7200


@dataclass(frozen=True)
class OrderRefundInput:
    order_id: str
    amount: Decimal
    reason: str
    actor: Actor
