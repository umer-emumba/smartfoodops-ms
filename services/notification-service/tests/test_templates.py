"""Who is told what, per event (app/templates.py)."""

import pytest
from app.templates import messages_for
from sfo_events import EventType

from tests.events import (
    CUSTOMER,
    OWNER,
    RIDER_USER,
    delivery_payload,
    event,
    order_payload,
    placed,
)


def _summary(ev):  # type: ignore[no-untyped-def]
    return [(m.user_id, m.recipient_role, m.type, m.channels) for m in messages_for(ev)]


def test_order_placed_tells_the_customer_without_a_premature_amount() -> None:
    [message] = messages_for(placed())
    assert (message.user_id, message.channels) == (CUSTOMER, ("in_app", "email"))
    assert "#1042" in message.content and "PKR" not in message.content


def test_restaurant_hears_of_new_orders_only_when_awaiting_acceptance() -> None:
    awaiting = event(EventType.ORDER_AWAITING_RESTAURANT, order_payload())
    assert _summary(awaiting) == [(OWNER, "restaurant_admin", "new_order", ("in_app", "sms"))]
    assert "PKR 1,250.00" in messages_for(awaiting)[0].content
    assert all(m.user_id != OWNER for m in messages_for(placed()))


@pytest.mark.parametrize(
    ("reason", "wording"),
    [
        ("payment_declined", "your payment was declined"),
        ("out_of_stock", "some items are out of stock"),
        ("restaurant_timeout", "didn't respond in time"),
        ("restaurant_rejected: kitchen closed", "the restaurant couldn't take it"),
        ("system_error", "something went wrong on our side"),
        ("something_new", "something went wrong on our side"),
    ],
)
def test_rejection_wording_by_reason(reason: str, wording: str) -> None:
    [message] = messages_for(event(EventType.ORDER_REJECTED, order_payload(reason=reason)))
    assert wording in message.content
    assert "kitchen closed" not in message.content  # the restaurant's own text isn't shown
    assert message.channels == ("in_app", "email")


def test_cancellation_reaches_the_restaurant_only_if_it_knew_of_the_order() -> None:
    early = event(EventType.ORDER_CANCELLED, order_payload(from_status="created"))
    assert [m.user_id for m in messages_for(early)] == [CUSTOMER]
    late = event(EventType.ORDER_CANCELLED, order_payload(from_status="confirmed"))
    assert _summary(late) == [
        (CUSTOMER, "customer", "cancellation", ("in_app", "email")),
        (OWNER, "restaurant_admin", "cancellation", ("in_app", "sms")),
    ]


def test_no_rider_cancellation_says_why() -> None:
    [message] = messages_for(
        event(
            EventType.ORDER_CANCELLED,
            order_payload(from_status="confirmed", reason="no_rider_available"),
        )
    )[:1]
    assert "no rider was available" in message.content


def test_kitchen_timeout_cancellation_says_why() -> None:
    [message] = messages_for(
        event(
            EventType.ORDER_CANCELLED,
            order_payload(from_status="assigned", reason="kitchen_timeout"),
        )
    )[:1]
    assert "the restaurant couldn't prepare it in time" in message.content


def test_delivered_tells_customer_and_restaurant() -> None:
    ev = event(
        EventType.ORDER_DELIVERED, order_payload(from_status="picked_up", to_status="delivered")
    )
    assert [m.user_id for m in messages_for(ev)] == [CUSTOMER, OWNER]


def test_order_without_restaurant_admin_skips_the_restaurant() -> None:
    ev = event(EventType.ORDER_AWAITING_RESTAURANT, order_payload(restaurant_admin_id=None))
    assert messages_for(ev) == []


@pytest.mark.parametrize(
    ("to_status", "expected"),
    [
        ("offered", [(RIDER_USER, "rider", "delivery_offer", ("in_app", "sms"))]),
        ("assigned", [(RIDER_USER, "rider", "delivery_update", ("in_app",))]),
        ("cancelled", [(RIDER_USER, "rider", "cancellation", ("in_app", "sms"))]),
        ("picked_up", []),
        ("pending_assignment", []),
    ],
)
def test_riders_hear_from_delivery_events(to_status: str, expected: list) -> None:  # type: ignore[type-arg]
    ev = event(EventType.DELIVERY_STATUS_UPDATED, delivery_payload(to_status=to_status))
    assert _summary(ev) == expected


def _food_ready(**overrides):  # type: ignore[no-untyped-def]
    payload = {
        "delivery_id": "d-1",
        "order_id": "o-1",
        "customer_id": CUSTOMER,
        "rider_id": "rider-1",
        "rider_user_id": RIDER_USER,
        "status": "assigned",
        "food_ready_at": "2026-10-26T12:00:00+00:00",
        **overrides,
    }
    return event(EventType.DELIVERY_FOOD_READY, payload)


def test_food_ready_tells_the_customer_and_the_assigned_rider() -> None:
    assert _summary(_food_ready()) == [
        (CUSTOMER, "customer", "order_update", ("in_app",)),
        (RIDER_USER, "rider", "delivery_update", ("in_app", "sms")),
    ]
    assert "your rider is picking it up" in messages_for(_food_ready())[0].content


def test_food_ready_before_a_rider_tells_only_the_customer() -> None:
    [message] = messages_for(_food_ready(status="pending_assignment", rider_user_id=None))
    assert message.user_id == CUSTOMER and "we're finding a rider" in message.content


def test_released_delivery_has_no_rider_to_tell() -> None:
    ev = event(
        EventType.DELIVERY_STATUS_UPDATED,
        delivery_payload(to_status="cancelled", rider_id=None, rider_user_id=None),
    )
    assert messages_for(ev) == []


def test_refund_amount_is_formatted() -> None:
    ev = event(
        EventType.REFUND_PROCESSED,
        {
            "payment_id": "p-1",
            "order_id": "o-1",
            "customer_id": CUSTOMER,
            "refund_id": "rf-1",
            "amount": "500.00",
            "total_refunded": "500.00",
            "reason": "cold",
        },
    )
    [message] = messages_for(ev)
    assert "PKR 500.00" in message.content and message.type == "refund"


def test_authorizations_and_shift_changes_notify_nobody() -> None:
    payment = event(
        EventType.PAYMENT_SUCCESSFUL,
        {
            "payment_id": "p-1",
            "order_id": "o-1",
            "customer_id": CUSTOMER,
            "amount": "10.00",
            "status": "authorized",
        },
    )
    shift = event(
        EventType.RIDER_SHIFT_CHANGED,
        {
            "rider_id": "rider-1",
            "rider_user_id": RIDER_USER,
            "from_status": "offline",
            "to_status": "available",
            "on_shift": True,
            "reason": "rider",
        },
    )
    assert messages_for(payment) == [] and messages_for(shift) == []
