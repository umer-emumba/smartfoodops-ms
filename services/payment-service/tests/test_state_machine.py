import itertools

import pytest
from app.state_machine import PaymentStatus as P
from app.state_machine import can_transition

ALLOWED = {
    (P.AUTHORIZED, P.CAPTURED),
    (P.AUTHORIZED, P.VOIDED),
    (P.CAPTURED, P.REFUNDED),
    (P.REFUNDED, P.REFUNDED),
}


@pytest.mark.parametrize("from_status,to_status", sorted(itertools.product(P, P)))
def test_only_the_payment_lifecycle_transitions_are_allowed(from_status: P, to_status: P) -> None:
    assert can_transition(from_status, to_status) == ((from_status, to_status) in ALLOWED)
