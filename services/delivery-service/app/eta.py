"""ETA estimates (docs/week2-plan.md step 5). No routing engine (plan §9):
straight-line distance × a road factor, at an average city speed per
vehicle. Placeholders like the pricing values — confirm with the mentor.

    pickup ETA  = max(now + rider → restaurant travel, food ready time)
    dropoff ETA = pickup ETA + restaurant → customer travel"""

from datetime import datetime, timedelta

from sfo_common.geo import haversine_km

# Straight lines are shorter than streets.
ROAD_FACTOR = 1.3
SPEED_KMH: dict[str, float] = {"bike": 12, "scooter": 25, "car": 30}
_DEFAULT_SPEED_KMH = 20.0


def travel_time(km: float, vehicle_type: str) -> timedelta:
    speed = SPEED_KMH.get(vehicle_type, _DEFAULT_SPEED_KMH)
    return timedelta(hours=km * ROAD_FACTOR / speed)


def estimate(
    *,
    now: datetime,
    rider_position: tuple[float, float] | None,
    pickup: tuple[float, float],
    dropoff: tuple[float, float],
    vehicle_type: str,
    ready_at: datetime | None,
) -> tuple[datetime, datetime]:
    """(pickup ETA, dropoff ETA). An unknown rider position counts as
    already at the restaurant (it was found within the search radius)."""
    to_pickup = (
        travel_time(haversine_km(*rider_position, *pickup), vehicle_type)
        if rider_position is not None
        else timedelta(0)
    )
    pickup_eta = now + to_pickup
    if ready_at is not None and ready_at > pickup_eta:
        pickup_eta = ready_at
    return pickup_eta, pickup_eta + travel_time(haversine_km(*pickup, *dropoff), vehicle_type)
