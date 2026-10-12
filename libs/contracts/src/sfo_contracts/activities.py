"""Activity names and their input/output types, one section per task queue
(docs/week2-plan.md §5). The workflow in order-service and the activity
implementations in each service both import from here, so a renamed field or
activity fails type-checking on both sides instead of at runtime.

IDs are strings (UUIDv7, sfo_common.ids); money is Decimal. Both survive the
round trip through Temporal via the pydantic data converter
(sfo_common.temporal.DATA_CONVERTER)."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

# ---------- Shared ----------

# Registered on every worker by sfo_common.temporal; used to check that a
# service's worker is up and polling its queue.
PING = "ping"


@dataclass(frozen=True)
class PingResult:
    service: str
    task_queue: str


@dataclass(frozen=True)
class OrderRef:
    """Input for activities that only need the order — release, complete,
    capture, void."""

    order_id: str


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float


# ---------- restaurant-activities ----------


class RestaurantActivity(StrEnum):
    RESERVE_ORDER = "reserve_order"
    RELEASE_RESERVATION = "release_reservation"  # compensates RESERVE_ORDER
    COMPLETE_RESERVATION = "complete_reservation"
    REDEEM_PROMOTION = "redeem_promotion"
    RELEASE_PROMOTION = "release_promotion"  # compensates REDEEM_PROMOTION


@dataclass(frozen=True)
class ReservedItem:
    menu_item_id: str
    quantity: int


@dataclass(frozen=True)
class ReserveOrderInput:
    order_id: str
    restaurant_id: str
    items: list[ReservedItem]


@dataclass(frozen=True)
class ReserveOrderResult:
    order_id: str
    status: str


@dataclass(frozen=True)
class RedeemPromotionInput:
    order_id: str
    restaurant_id: str
    code: str


@dataclass(frozen=True)
class RedeemPromotionResult:
    promotion_id: str
    discount_type: Literal["percentage", "fixed_amount"]
    discount_value: Decimal


# ---------- payment-activities ----------


class PaymentActivity(StrEnum):
    AUTHORIZE = "authorize"
    CAPTURE = "capture"
    VOID = "void"  # compensates AUTHORIZE before capture
    REFUND = "refund"  # compensates CAPTURE


PaymentMethod = Literal["card_simulated", "card_simulated_decline"]


@dataclass(frozen=True)
class AuthorizePaymentInput:
    order_id: str
    customer_id: str
    amount: Decimal
    method: PaymentMethod = "card_simulated"


@dataclass(frozen=True)
class PaymentResult:
    payment_id: str
    status: str


@dataclass(frozen=True)
class RefundPaymentInput:
    order_id: str
    # The refund workflow's ID — makes the refund idempotent across retries.
    refund_id: str
    reason: str
    # None = refund everything captured and not yet refunded.
    amount: Decimal | None = None


@dataclass(frozen=True)
class RefundPaymentResult:
    payment_id: str
    refunded_amount: Decimal
    total_refunded: Decimal


# ---------- order-activities ----------


class OrderActivity(StrEnum):
    TRANSITION_ORDER = "transition_order"
    APPLY_PRICING = "apply_pricing"
    RECORD_REFUND = "record_refund"


@dataclass(frozen=True)
class Actor:
    """Who caused a change, for the audit trail. Both None = the saga itself
    (system)."""

    id: str | None = None
    role: str | None = None


SYSTEM = Actor()


@dataclass(frozen=True)
class TransitionOrderInput:
    order_id: str
    to_status: str
    actor: Actor = SYSTEM
    reason: str | None = None


@dataclass(frozen=True)
class TransitionOrderResult:
    status: str
    # False when the order was already in `to_status` (idempotent re-run).
    changed: bool


@dataclass(frozen=True)
class ApplyPricingInput:
    order_id: str
    discount: Decimal
    tax: Decimal
    delivery_fee: Decimal


@dataclass(frozen=True)
class ApplyPricingResult:
    total_amount: Decimal


@dataclass(frozen=True)
class RecordRefundInput:
    order_id: str
    amount: Decimal
    reason: str
    actor: Actor


# ---------- delivery-activities ----------


class DeliveryActivity(StrEnum):
    CREATE_DELIVERY = "create_delivery"
    CANCEL_DELIVERY = "cancel_delivery"  # compensates CREATE_DELIVERY; takes OrderRef
    OFFER_TO_NEAREST_RIDER = "offer_to_nearest_rider"
    RELEASE_RIDER = "release_rider"  # compensates OFFER_TO_NEAREST_RIDER
    # Rider accepted (→ assigned), picked up, delivered — rider endpoints
    # signal the saga, the saga makes the change.
    TRANSITION_DELIVERY = "transition_delivery"
    COMPUTE_ETA = "compute_eta"


class RiderReleaseOutcome(StrEnum):
    DECLINED = "declined"
    EXPIRED = "expired"
    DROPPED = "dropped"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True)
class DeliveryRef:
    delivery_id: str


@dataclass(frozen=True)
class CreateDeliveryInput:
    order_id: str
    customer_id: str  # who may see the delivery (GET /v1/deliveries/by-order/{id})
    pickup: GeoPoint
    dropoff: GeoPoint
    # When the food is expected to be ready — the pickup ETA is never earlier.
    ready_at: datetime | None = None


@dataclass(frozen=True)
class CreateDeliveryResult:
    delivery_id: str
    # "cancelled" if the delivery was cancelled before it was created (a
    # compensation that ran first) — the saga must not use it.
    status: str


@dataclass(frozen=True)
class OfferRiderInput:
    delivery_id: str
    exclude_rider_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RiderOffer:
    """`offer_to_nearest_rider` returns this, or None when no rider is free."""

    rider_id: str
    distance_km: float  # rider → pickup, straight line
    expires_at: datetime


@dataclass(frozen=True)
class TransitionDeliveryInput:
    delivery_id: str
    to_status: Literal["assigned", "picked_up", "delivered"]


@dataclass(frozen=True)
class ReleaseRiderInput:
    delivery_id: str
    outcome: RiderReleaseOutcome


@dataclass(frozen=True)
class DeliveryEta:
    pickup_eta: datetime
    dropoff_eta: datetime
