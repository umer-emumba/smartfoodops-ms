"""Who is told what for each domain event (docs/week3-plan.md, event →
consumer map). Every message goes in-app; the ones a person must act on or
would want a record of also go by email or SMS.

Recipients come from the event payloads (W3-D8), so nothing here calls
another service. The restaurant hears about a new order from
OrderAwaitingRestaurant — by then stock is reserved and payment authorized
(W3-D8a) — and riders only from delivery events (W3-D8b)."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sfo_events import DomainEvent, EventType
from sfo_events.payloads import (
    DeliveryStatusUpdatedPayload,
    OrderPlacedPayload,
    OrderTransitionPayload,
    RefundProcessedPayload,
)

CUSTOMER, RESTAURANT_ADMIN, RIDER = "customer", "restaurant_admin", "rider"
IN_APP, EMAIL, SMS = "in_app", "email", "sms"


@dataclass(frozen=True)
class Message:
    user_id: str
    recipient_role: str
    type: str  # order_update | new_order | cancellation | refund | delivery_offer | delivery_update
    channels: tuple[str, ...]
    content: str
    order_id: str | None


# Why an order was rejected, as the customer is told it. `restaurant_rejected`
# may carry the restaurant's own text after a colon — not shown.
_REJECTION_WORDING = {
    "out_of_stock": "some items are out of stock",
    "restaurant_at_capacity": "the restaurant is too busy right now",
    "restaurant_unavailable": "the restaurant isn't taking orders right now",
    "promotion_invalid": "the promo code couldn't be applied",
    "payment_declined": "your payment was declined",
    "restaurant_timeout": "the restaurant didn't respond in time",
    "restaurant_rejected": "the restaurant couldn't take it",
}
_FALLBACK_REJECTION = "something went wrong on our side"

# Order statuses in which the restaurant knows about the order — only then is
# it told about a cancellation.
_RESTAURANT_INVOLVED = {"awaiting_restaurant_acceptance", "confirmed", "assigned", "picked_up"}


def _pkr(amount: Decimal) -> str:
    return f"PKR {amount:,.2f}"


def _order(p: OrderPlacedPayload | OrderTransitionPayload) -> str:
    return f"Order #{p.order_number}"


def _to_customer(
    p: OrderPlacedPayload | OrderTransitionPayload,
    type_: str,
    content: str,
    *channels: str,
) -> Message:
    return Message(p.customer_id, CUSTOMER, type_, (IN_APP, *channels), content, p.order_id)


def _to_restaurant(
    p: OrderTransitionPayload, type_: str, content: str, *channels: str
) -> list[Message]:
    if p.restaurant_admin_id is None:
        return []
    return [
        Message(
            p.restaurant_admin_id, RESTAURANT_ADMIN, type_, (IN_APP, *channels), content, p.order_id
        )
    ]


def _order_placed(p: OrderPlacedPayload) -> list[Message]:
    # No amount: at this point total_amount is still the subtotal — tax and
    # delivery fee are added by the saga before the restaurant is asked.
    return [
        _to_customer(
            p,
            "order_update",
            f"{_order(p)} received — we're confirming it with the restaurant.",
            EMAIL,
        )
    ]


def _awaiting_restaurant(p: OrderTransitionPayload) -> list[Message]:
    return _to_restaurant(
        p,
        "new_order",
        f"New order: {_order(p)} ({_pkr(p.total_amount)}). Please accept or reject it.",
        SMS,
    )


def _confirmed(p: OrderTransitionPayload) -> list[Message]:
    return [
        _to_customer(p, "order_update", f"{_order(p)} confirmed — the restaurant is preparing it.")
    ]


def _rejected(p: OrderTransitionPayload) -> list[Message]:
    code = (p.reason or "").split(":", 1)[0]
    why = _REJECTION_WORDING.get(code, _FALLBACK_REJECTION)
    return [
        _to_customer(
            p,
            "cancellation",
            f"Sorry — {_order(p)} couldn't be placed: {why}. You haven't been charged.",
            EMAIL,
        )
    ]


def _cancelled(p: OrderTransitionPayload) -> list[Message]:
    why = ", as no rider was available" if p.reason == "no_rider_available" else ""
    messages = [
        _to_customer(
            p,
            "cancellation",
            f"{_order(p)} has been cancelled{why}. Any payment hold has been released.",
            EMAIL,
        )
    ]
    if p.from_status in _RESTAURANT_INVOLVED:
        messages += _to_restaurant(
            p, "cancellation", f"{_order(p)} was cancelled — please stop preparing it.", SMS
        )
    return messages


def _rider_assigned(p: OrderTransitionPayload) -> list[Message]:
    return [
        _to_customer(p, "order_update", f"A rider is heading to the restaurant for {_order(p)}.")
    ]


def _picked_up(p: OrderTransitionPayload) -> list[Message]:
    return [
        _to_customer(p, "order_update", f"{_order(p)} has been picked up and is on its way.", SMS)
    ]


def _delivered(p: OrderTransitionPayload) -> list[Message]:
    return [
        _to_customer(p, "order_update", f"{_order(p)} has been delivered. Enjoy your meal!", EMAIL),
        *_to_restaurant(p, "order_update", f"{_order(p)} was delivered."),
    ]


def _refund(p: RefundProcessedPayload) -> list[Message]:
    return [
        Message(
            p.customer_id,
            CUSTOMER,
            "refund",
            (IN_APP, EMAIL),
            f"A refund of {_pkr(p.amount)} has been issued for your order.",
            p.order_id,
        )
    ]


_RIDER_MESSAGES = {
    "offered": ("delivery_offer", "New delivery offer — open the app to accept it.", (IN_APP, SMS)),
    "assigned": (
        "delivery_update",
        "Delivery confirmed — head to the restaurant for pickup.",
        (IN_APP,),
    ),
    "cancelled": (
        "cancellation",
        "The delivery was cancelled — please don't pick it up.",
        (IN_APP, SMS),
    ),
}


def _delivery_status(p: DeliveryStatusUpdatedPayload) -> list[Message]:
    entry = _RIDER_MESSAGES.get(p.to_status)
    if entry is None or p.rider_user_id is None:
        return []
    type_, content, channels = entry
    return [Message(p.rider_user_id, RIDER, type_, channels, content, p.order_id)]


_BUILDERS: dict[EventType, Callable[..., list[Message]]] = {
    EventType.ORDER_PLACED: _order_placed,
    EventType.ORDER_AWAITING_RESTAURANT: _awaiting_restaurant,
    EventType.ORDER_CONFIRMED: _confirmed,
    EventType.ORDER_REJECTED: _rejected,
    EventType.ORDER_CANCELLED: _cancelled,
    EventType.RIDER_ASSIGNED: _rider_assigned,
    EventType.ORDER_PICKED_UP: _picked_up,
    EventType.ORDER_DELIVERED: _delivered,
    EventType.REFUND_PROCESSED: _refund,
    EventType.DELIVERY_STATUS_UPDATED: _delivery_status,
    # PaymentSuccessful (an authorization isn't worth a message) and
    # RiderShiftChanged (analytics only): nothing.
}


def messages_for(event: DomainEvent) -> list[Message]:
    builder = _BUILDERS.get(event.event_type)
    return builder(event.typed_payload()) if builder else []
