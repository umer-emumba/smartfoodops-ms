"""The order lifecycle as data (docs/week2-plan.md step 1):

    created → awaiting_restaurant_acceptance → confirmed → assigned → picked_up → delivered
    → rejected   from created / awaiting_restaurant_acceptance
    → cancelled  from any state before picked_up

Only the saga's transition_order activity moves an order between statuses;
API endpoints check `can_cancel` / the current status to fail fast with 409,
then signal the saga."""

from app.models import OrderStatus

S = OrderStatus

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.CREATED: frozenset({S.AWAITING_RESTAURANT_ACCEPTANCE, S.REJECTED, S.CANCELLED}),
    S.AWAITING_RESTAURANT_ACCEPTANCE: frozenset({S.CONFIRMED, S.REJECTED, S.CANCELLED}),
    S.CONFIRMED: frozenset({S.ASSIGNED, S.CANCELLED}),
    S.ASSIGNED: frozenset({S.PICKED_UP, S.CANCELLED}),
    S.PICKED_UP: frozenset({S.DELIVERED}),
    S.DELIVERED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL: frozenset[OrderStatus] = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)

# The timestamp column each status sets when the order enters it.
ENTERED_AT_COLUMN: dict[OrderStatus, str] = {
    S.CONFIRMED: "confirmed_at",
    S.ASSIGNED: "assigned_at",
    S.PICKED_UP: "picked_up_at",
    S.DELIVERED: "delivered_at",
    S.REJECTED: "rejected_at",
    S.CANCELLED: "cancelled_at",
}


def can_transition(from_status: str, to_status: str) -> bool:
    return OrderStatus(to_status) in ALLOWED_TRANSITIONS[OrderStatus(from_status)]


def can_cancel(status: str) -> bool:
    return can_transition(status, S.CANCELLED)
