"""The metrics (docs/week3-plan.md, "Metric definitions"), computed from the
read model at request time.

Orders are counted in the window they were *placed* in. Rates are None
when their denominator is zero (no data isn't 0%).

Rider utilization = time on deliveries ÷ time on shift, over riders whose
shift changes are known. Time on a delivery runs from the (latest) rider's
acceptance to delivery or cancellation; a rider who dropped out of a
delivery isn't credited for it. A rider already on shift when the first
event was recorded (only an "off" is known) counts as on shift from the
start of the window. Both are computed in Python over the window's rows —
fine at this scale; at scale they'd become pipelines or precomputed rollups.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.facts import RESTAURANT_REJECTIONS
from app.mongo import FAILED_EVENTS, ORDER_FACTS, RIDER_SHIFT_FACTS


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def clip(self, start: datetime, end: datetime) -> float:
        """Seconds of [start, end) inside the window."""
        return max(0.0, (min(end, self.end) - max(start, self.start)).total_seconds())


def _has(field: str) -> dict[str, Any]:
    return {"$gt": [f"${field}", None]}


def _count_if(condition: dict[str, Any]) -> dict[str, Any]:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _placed_in(window: Window, restaurant_id: str | None) -> dict[str, Any]:
    match: dict[str, Any] = {"placed_at": {"$gte": window.start, "$lt": window.end}}
    if restaurant_id is not None:
        match["restaurant_id"] = restaurant_id
    return match


async def order_stats(
    db: AsyncDatabase, window: Window, restaurant_id: str | None = None
) -> dict[str, Any]:
    reject_reason = {"$ifNull": ["$reject_reason", ""]}
    pipeline = [
        {"$match": _placed_in(window, restaurant_id)},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "cancelled": _count_if(_has("cancelled_at")),
                "rejected": _count_if(_has("rejected_at")),
                "confirmed": _count_if(_has("confirmed_at")),
                "restaurant_rejected": _count_if(
                    {"$in": [reject_reason, list(RESTAURANT_REJECTIONS)]}
                ),
                "system_errors": _count_if(
                    {
                        "$or": [
                            {"$eq": ["$reject_reason", "system_error"]},
                            {"$eq": ["$cancel_reason", "system_error"]},
                        ]
                    }
                ),
                "deliveries_started": _count_if(_has("delivery_assigned_at")),
                "deliveries_completed": _count_if(
                    {"$and": [_has("delivery_assigned_at"), _has("delivered_at")]}
                ),
                "delivery_seconds": {
                    "$avg": {
                        "$cond": [
                            _has("delivered_at"),
                            {"$divide": [{"$subtract": ["$delivered_at", "$placed_at"]}, 1000]},
                            None,
                        ]
                    }
                },
            }
        },
    ]
    rows = await (await db[ORDER_FACTS].aggregate(pipeline)).to_list()
    s = rows[0] if rows else {}
    total = s.get("total", 0)
    confirmed = s.get("confirmed", 0)
    avg_seconds = s.get("delivery_seconds")
    return {
        "total_orders": total,
        "confirmed": confirmed,
        "rejected": s.get("rejected", 0),
        "cancelled": s.get("cancelled", 0),
        "delivered": s.get("deliveries_completed", 0),
        "cancellation_rate": _rate(s.get("cancelled", 0), total),
        "restaurant_acceptance_rate": _rate(confirmed, confirmed + s.get("restaurant_rejected", 0)),
        "delivery_success_rate": _rate(
            s.get("deliveries_completed", 0), s.get("deliveries_started", 0)
        ),
        "avg_delivery_minutes": round(avg_seconds / 60, 2) if avg_seconds is not None else None,
        "saga_system_errors": s.get("system_errors", 0),
    }


async def orders_per_restaurant(db: AsyncDatabase, window: Window) -> list[dict[str, Any]]:
    pipeline = [
        {"$match": _placed_in(window, None)},
        {"$group": {"_id": "$restaurant_id", "orders": {"$sum": 1}}},
        {"$sort": {"orders": -1, "_id": 1}},
    ]
    rows = await (await db[ORDER_FACTS].aggregate(pipeline)).to_list()
    return [{"restaurant_id": r["_id"], "orders": r["orders"]} for r in rows]


async def peak_hours(
    db: AsyncDatabase, window: Window, timezone: str, restaurant_id: str | None = None
) -> list[dict[str, int]]:
    """Orders per hour of the day (0–23, in `timezone`), all 24 hours."""
    pipeline = [
        {"$match": _placed_in(window, restaurant_id)},
        {
            "$group": {
                "_id": {"$hour": {"date": "$placed_at", "timezone": timezone}},
                "orders": {"$sum": 1},
            }
        },
    ]
    counts = {
        r["_id"]: r["orders"] for r in await (await db[ORDER_FACTS].aggregate(pipeline)).to_list()
    }
    return [{"hour": hour, "orders": counts.get(hour, 0)} for hour in range(24)]


async def failed_events(db: AsyncDatabase, window: Window) -> int:
    return await db[FAILED_EVENTS].count_documents(
        {"at": {"$gte": window.start, "$lt": window.end}}
    )


async def _seconds_on_shift(db: AsyncDatabase, window: Window, now: datetime) -> dict[str, float]:
    on_shift: dict[str, float] = {}
    since: dict[str, datetime | None] = {}
    cursor = (
        db[RIDER_SHIFT_FACTS].find({"at": {"$lt": window.end}}).sort([("rider_id", 1), ("at", 1)])
    )
    async for change in cursor:
        rider = change["rider_id"]
        on_shift.setdefault(rider, 0.0)
        opened = since.get(rider)
        if change["on_shift"]:
            if opened is None:
                since[rider] = change["at"]
        else:
            # No "on" recorded: on shift since before records began.
            on_shift[rider] += window.clip(opened or window.start, change["at"])
            since[rider] = None
    for rider, start in since.items():
        if start is not None:
            on_shift[rider] += window.clip(start, now)
    return on_shift


async def _seconds_on_deliveries(
    db: AsyncDatabase, window: Window, now: datetime
) -> dict[str, float]:
    busy: dict[str, float] = {}
    cursor = db[ORDER_FACTS].find(
        {
            "delivery_rider_id": {"$exists": True},
            "delivery_assigned_at": {"$lt": window.end},
            "$or": [{"delivery_ended_at": None}, {"delivery_ended_at": {"$gte": window.start}}],
        },
        {"delivery_rider_id": 1, "delivery_assigned_at": 1, "delivery_ended_at": 1},
    )
    async for fact in cursor:
        rider = fact["delivery_rider_id"]
        end = fact.get("delivery_ended_at") or now
        busy[rider] = busy.get(rider, 0.0) + window.clip(fact["delivery_assigned_at"], end)
    return busy


async def rider_utilization(
    db: AsyncDatabase, window: Window, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    shift = await _seconds_on_shift(db, window, now)
    busy = await _seconds_on_deliveries(db, window, now)
    riders = [
        {
            "rider_id": rider,
            "on_shift_seconds": round(seconds, 1),
            "on_delivery_seconds": round(busy.get(rider, 0.0), 1),
            "utilization": _rate(busy.get(rider, 0.0), seconds) if seconds else None,
        }
        for rider, seconds in sorted(shift.items())
    ]
    total_shift = sum(shift.values())
    total_busy = sum(busy.get(rider, 0.0) for rider in shift)
    return {
        "overall": round(total_busy / total_shift, 4) if total_shift else None,
        "riders": riders,
    }
