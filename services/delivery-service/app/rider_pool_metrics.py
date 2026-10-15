"""Rider pool size by status for Grafana (week3-plan step 5): approved
riders that are available, busy (holding an offer or a delivery) or
offline. Sampled every 15s next to the Temporal worker — a gauge read from
Postgres, so it's right even after a restart."""

import asyncio
import logging

from prometheus_client import Gauge
from sfo_observability import quiet
from sqlalchemy import func, select
from temporalio.client import Client

from app.db import SessionFactory
from app.models import Rider

_logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 15
_STATUSES = ("available", "busy", "offline")

RIDERS = Gauge("riders", "Approved riders by status", ["status"])


async def sample_once() -> None:
    with quiet():
        async with SessionFactory() as session:
            rows = await session.execute(
                select(Rider.status, func.count())
                .where(Rider.verification_status == "approved")
                .group_by(Rider.status)
            )
            counts = {status: n for status, n in rows.all()}
    for status in _STATUSES:
        RIDERS.labels(status).set(counts.get(status, 0))


async def run_rider_pool_gauge(_: Client) -> None:
    while True:
        try:
            await sample_once()
        except Exception:
            _logger.warning("Rider pool sample failed", exc_info=True)
        await asyncio.sleep(_INTERVAL_SECONDS)
