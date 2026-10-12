"""The payment lifecycle as data (docs/week2-plan.md step 3):

    authorized → captured → refunded     (refunds may be partial)
    authorized → voided                  (released before capture)
    declined                             (authorization refused; terminal)

Money is only taken at capture, on delivery — so every cancellation before
delivery is a void, and a refund only happens after delivery (D13)."""

from enum import StrEnum


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"
    DECLINED = "declined"


P = PaymentStatus

ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    P.AUTHORIZED: frozenset({P.CAPTURED, P.VOIDED}),
    P.CAPTURED: frozenset({P.REFUNDED}),
    # A further partial refund keeps it `refunded`.
    P.REFUNDED: frozenset({P.REFUNDED}),
    P.VOIDED: frozenset(),
    P.DECLINED: frozenset(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return PaymentStatus(to_status) in ALLOWED_TRANSITIONS[PaymentStatus(from_status)]
