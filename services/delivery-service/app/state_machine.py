"""The delivery lifecycle as data (docs/week2-plan.md step 5):

    pending_assignment → offered → assigned → picked_up → delivered
    offered / assigned → pending_assignment   (declined, expired, rider dropped out)
    → cancelled                               (any state before picked_up)

Only the order saga's activities (app/activities.py) change a delivery's
status; rider endpoints check it for a fast 409, then signal the saga."""

from enum import StrEnum


class DeliveryStatus(StrEnum):
    PENDING_ASSIGNMENT = "pending_assignment"
    OFFERED = "offered"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


D = DeliveryStatus

ALLOWED_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    D.PENDING_ASSIGNMENT: frozenset({D.OFFERED, D.CANCELLED}),
    D.OFFERED: frozenset({D.ASSIGNED, D.PENDING_ASSIGNMENT, D.CANCELLED}),
    D.ASSIGNED: frozenset({D.PICKED_UP, D.PENDING_ASSIGNMENT, D.CANCELLED}),
    D.PICKED_UP: frozenset({D.DELIVERED}),
    D.DELIVERED: frozenset(),
    D.CANCELLED: frozenset(),
}

# Statuses in which a rider is attached to the delivery.
WITH_RIDER: frozenset[DeliveryStatus] = frozenset({D.OFFERED, D.ASSIGNED, D.PICKED_UP})


def can_transition(from_status: str, to_status: str) -> bool:
    return DeliveryStatus(to_status) in ALLOWED_TRANSITIONS[DeliveryStatus(from_status)]
