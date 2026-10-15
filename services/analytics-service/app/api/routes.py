"""Analytics over a time window (docs/week3-plan.md step 3). `from`/`to`
default to the last 7 days; times without a timezone are UTC. Orders count
in the window they were placed in.

- `GET /v1/analytics/summary` — system admin: every metric.
- `GET /v1/analytics/restaurants/{id}` — system admin, or the admin of that
  restaurant (ownership from `restaurant_admin_id` on its orders' facts).
- `GET /v1/analytics/peak-hours` — system admin: orders per hour of day.

Failed notification sends aren't here: they live only in
notification-service, and are on the Grafana dashboards (Prometheus)."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ForbiddenError, ValidationAppError

from app import mongo, queries
from app.auth_deps import require_roles
from app.config import settings
from app.queries import Window
from app.schemas import (
    PeakHoursResponse,
    RestaurantAnalyticsResponse,
    SummaryResponse,
)

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])

_DEFAULT_WINDOW = timedelta(days=7)


def _window(
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> Window:
    end = _utc(end) if end else datetime.now(UTC)
    start = _utc(start) if start else end - _DEFAULT_WINDOW
    if start >= end:
        raise ValidationAppError("`from` must be before `to`")
    return Window(start, end)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _window_body(window: Window) -> dict[str, datetime]:
    return {"start": window.start, "end": window.end}


@router.get("/summary", response_model=SummaryResponse)
async def summary(
    window: Window = Depends(_window),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> dict:
    db = mongo.db
    orders, per_restaurant, hours, utilization, dead_lettered = await asyncio.gather(
        queries.order_stats(db, window),
        queries.orders_per_restaurant(db, window),
        queries.peak_hours(db, window, settings.analytics_timezone),
        queries.rider_utilization(db, window),
        queries.failed_events(db, window),
    )
    return {
        "window": _window_body(window),
        "orders": orders,
        "orders_per_restaurant": per_restaurant,
        "peak_hours": hours,
        "rider_utilization": utilization,
        "failed_events": {
            "dead_lettered": dead_lettered,
            "saga_system_errors": orders["saga_system_errors"],
            "total": dead_lettered + orders["saga_system_errors"],
        },
    }


@router.get("/restaurants/{restaurant_id}", response_model=RestaurantAnalyticsResponse)
async def restaurant(
    restaurant_id: str,
    window: Window = Depends(_window),
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN, Role.RESTAURANT_ADMIN)),
) -> dict:
    db = mongo.db
    if current.role == Role.RESTAURANT_ADMIN:
        owns = await db[mongo.ORDER_FACTS].find_one(
            {"restaurant_id": restaurant_id, "restaurant_admin_id": current.id}, {"_id": 1}
        )
        if owns is None:
            raise ForbiddenError("Not your restaurant")
    orders, hours = await asyncio.gather(
        queries.order_stats(db, window, restaurant_id),
        queries.peak_hours(db, window, settings.analytics_timezone, restaurant_id),
    )
    return {
        "restaurant_id": restaurant_id,
        "window": _window_body(window),
        "orders": orders,
        "peak_hours": hours,
    }


@router.get("/peak-hours", response_model=PeakHoursResponse)
async def peak_hours(
    window: Window = Depends(_window),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
) -> dict:
    return {
        "window": _window_body(window),
        "timezone": settings.analytics_timezone,
        "peak_hours": await queries.peak_hours(mongo.db, window, settings.analytics_timezone),
    }
