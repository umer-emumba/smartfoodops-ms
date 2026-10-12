import itertools
from datetime import UTC, datetime, timedelta

import pytest
from app import eta
from app.state_machine import DeliveryStatus as D
from app.state_machine import can_transition

ALLOWED = {
    (D.PENDING_ASSIGNMENT, D.OFFERED),
    (D.PENDING_ASSIGNMENT, D.CANCELLED),
    (D.OFFERED, D.ASSIGNED),
    (D.OFFERED, D.PENDING_ASSIGNMENT),
    (D.OFFERED, D.CANCELLED),
    (D.ASSIGNED, D.PICKED_UP),
    (D.ASSIGNED, D.PENDING_ASSIGNMENT),
    (D.ASSIGNED, D.CANCELLED),
    (D.PICKED_UP, D.DELIVERED),
}


@pytest.mark.parametrize("from_status,to_status", sorted(itertools.product(D, D)))
def test_only_the_delivery_lifecycle_transitions_are_allowed(from_status: D, to_status: D) -> None:
    assert can_transition(from_status, to_status) == ((from_status, to_status) in ALLOWED)


@pytest.mark.parametrize(
    "vehicle,minutes", [("bike", 6.5), ("scooter", 3.12), ("car", 2.6), ("hoverboard", 3.9)]
)
def test_travel_time_per_vehicle(vehicle: str, minutes: float) -> None:
    # 1 km straight line × 1.3 road factor at 12 / 25 / 30 / 20 (default) km/h.
    assert eta.travel_time(1, vehicle).total_seconds() / 60 == pytest.approx(minutes, abs=0.01)


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
PICKUP = (24.8138, 67.0300)
DROPOFF = (24.8138 + 2 / 111.2, 67.0300)  # ~2 km north


def test_eta_rider_travel_then_drop_off_leg() -> None:
    rider = (24.8138 + 1 / 111.2, 67.0300)  # ~1 km away
    pickup_eta, dropoff_eta = eta.estimate(
        now=NOW,
        rider_position=rider,
        pickup=PICKUP,
        dropoff=DROPOFF,
        vehicle_type="bike",
        ready_at=None,
    )
    assert pickup_eta - NOW == pytest.approx(timedelta(minutes=6.5), abs=timedelta(seconds=2))
    assert dropoff_eta - pickup_eta == pytest.approx(
        timedelta(minutes=13), abs=timedelta(seconds=3)
    )


def test_pickup_never_before_the_food_is_ready() -> None:
    ready = NOW + timedelta(minutes=20)
    pickup_eta, _ = eta.estimate(
        now=NOW,
        rider_position=PICKUP,
        pickup=PICKUP,
        dropoff=DROPOFF,
        vehicle_type="car",
        ready_at=ready,
    )
    assert pickup_eta == ready


def test_unknown_rider_position_counts_as_at_the_restaurant() -> None:
    pickup_eta, _ = eta.estimate(
        now=NOW,
        rider_position=None,
        pickup=PICKUP,
        dropoff=DROPOFF,
        vehicle_type="bike",
        ready_at=None,
    )
    assert pickup_eta == NOW
