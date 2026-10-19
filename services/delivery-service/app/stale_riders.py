"""Stale-rider detector (docs/week2-plan.md D12): an assigned rider who has
sent no location update for 3 minutes (riders send one every 5–15s, so
that's a dozen or more missed) is treated as dropped out — the order saga is
signalled `rider_dropped` and finds another rider. Runs every 30s next to
the workers (app/worker.py).

Each (delivery, rider) pair is signalled once: a Redis marker blocks repeats
while the saga reacts. A rider who has picked the food up is never
signalled — after pickup it's a support case, not a reassignment."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sfo_common import temporal
from sfo_common.errors import AppError
from sfo_contracts import OrderSagaSignal
from sfo_contracts.workflows import RiderDropped
from sfo_observability import quiet
from sqlalchemy import or_, select
from temporalio.client import Client

from app import redis_client
from app.config import settings
from app.db import SessionFactory
from app.models import Delivery, Rider
from app.state_machine import DeliveryStatus

_logger = logging.getLogger(__name__)
_MARKER_TTL_SECONDS = 600
REASON = "no_location_updates"


def _marker(delivery_id: str, rider_id: str) -> str:
    return f"stale-rider-signalled:{delivery_id}:{rider_id}"


async def check_once(client: Client) -> int:
    """Signals the saga for every newly stale assigned rider. Returns how
    many were signalled."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.rider_stale_after_seconds)
    async with SessionFactory() as session:
        result = await session.execute(
            select(Delivery.id, Delivery.order_id, Delivery.rider_id)
            .join(Rider, Rider.id == Delivery.rider_id)
            .where(
                Delivery.status == DeliveryStatus.ASSIGNED,
                # Give a newly assigned rider the full window too.
                Delivery.assigned_at < cutoff,
                or_(Rider.last_location_at.is_(None), Rider.last_location_at < cutoff),
            )
        )
        stale = result.all()
    signalled = 0
    for delivery_id, order_id, rider_id in stale:
        if not await redis_client.redis.set(
            _marker(delivery_id, rider_id), "1", nx=True, ex=_MARKER_TTL_SECONDS
        ):
            continue
        try:
            # Tells the order's saga this rider dropped out; it frees them and
            # looks for another rider.
            await temporal.signal_order_saga(
                client,
                order_id,
                OrderSagaSignal.RIDER_DROPPED,
                RiderDropped(delivery_id=delivery_id, rider_id=rider_id, reason=REASON),
            )
            signalled += 1
            _logger.warning(
                "Rider stopped sending locations; reassigning",
                extra={"extra_fields": {"delivery_id": delivery_id, "rider_id": rider_id}},
            )
        except AppError:
            # The saga is gone (order finished meanwhile) — nothing to do.
            _logger.info(
                "Stale rider: no running saga", extra={"extra_fields": {"order_id": order_id}}
            )
    return signalled


async def run_stale_rider_detector(client: Client) -> None:
    while True:
        await asyncio.sleep(settings.stale_rider_check_interval_seconds)
        try:
            with quiet():  # a poll; signalled sagas are traced by Temporal
                await check_once(client)
        except Exception:
            _logger.warning("Stale-rider check failed", exc_info=True)
