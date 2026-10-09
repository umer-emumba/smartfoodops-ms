import itertools

import pytest
from app.models import OrderStatus as S
from app.state_machine import TERMINAL, can_cancel, can_transition

ALLOWED = {
    (S.CREATED, S.AWAITING_RESTAURANT_ACCEPTANCE),
    (S.CREATED, S.REJECTED),
    (S.CREATED, S.CANCELLED),
    (S.AWAITING_RESTAURANT_ACCEPTANCE, S.CONFIRMED),
    (S.AWAITING_RESTAURANT_ACCEPTANCE, S.REJECTED),
    (S.AWAITING_RESTAURANT_ACCEPTANCE, S.CANCELLED),
    (S.CONFIRMED, S.ASSIGNED),
    (S.CONFIRMED, S.CANCELLED),
    (S.ASSIGNED, S.PICKED_UP),
    (S.ASSIGNED, S.CANCELLED),
    (S.PICKED_UP, S.DELIVERED),
}


@pytest.mark.parametrize("from_status,to_status", sorted(itertools.product(S, S)))
def test_only_the_lifecycle_transitions_are_allowed(from_status: S, to_status: S) -> None:
    assert can_transition(from_status, to_status) == ((from_status, to_status) in ALLOWED)


def test_cancellable_until_pickup() -> None:
    assert {s for s in S if can_cancel(s)} == {
        S.CREATED,
        S.AWAITING_RESTAURANT_ACCEPTANCE,
        S.CONFIRMED,
        S.ASSIGNED,
    }


def test_terminal_statuses() -> None:
    assert TERMINAL == {S.DELIVERED, S.REJECTED, S.CANCELLED}
