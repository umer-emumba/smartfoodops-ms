"""Closes the gap between committing an order and starting its saga
(docs/week2-plan.md D4): POST /v1/orders commits, then starts workflow
`order-{id}`. If the process dies — or Temporal is down — in between, the
order would sit in `created` forever. Every 30s this finds such orders and
starts their sagas; starting one that's already running is a no-op, so every
replica can run it."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sfo_observability import quiet
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from temporalio.client import Client

from app import temporal_client
from app.config import settings
from app.db import SessionFactory
from app.models import Order, OrderStatus

_logger = logging.getLogger(__name__)
_BATCH_SIZE = 100


async def reconcile_once(client: Client) -> int:
    """Starts sagas for `created` orders between the min and max age.
    Returns how many were started."""
    now = datetime.now(UTC)
    newest = now - timedelta(seconds=settings.reconcile_min_age_seconds)
    oldest = now - timedelta(seconds=settings.reconcile_max_age_seconds)
    async with SessionFactory() as session:
        result = await session.execute(
            select(Order)
            .where(
                Order.status == OrderStatus.CREATED,
                Order.created_at < newest,
                Order.created_at >= oldest,
            )
            .order_by(Order.created_at)
            .limit(_BATCH_SIZE)
            .options(selectinload(Order.items))
        )
        orders = list(result.scalars().all())
    started = 0
    for order in orders:
        if await temporal_client.start_order_saga(client, order):
            started += 1
    if started:
        _logger.warning(
            "Started sagas for stranded orders", extra={"extra_fields": {"count": started}}
        )
    return started


async def count_abandoned() -> int:
    """`created` orders too old for the reconciler — left for an operator."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.reconcile_max_age_seconds)
    async with SessionFactory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.status == OrderStatus.CREATED, Order.created_at < cutoff)
            )
        ).scalar_one()


async def run_reconciler(client: Client) -> None:
    """Background task next to the workers (app/worker.py)."""
    try:
        abandoned = await count_abandoned()
        if abandoned:
            _logger.warning(
                "Orders stuck in `created` beyond the reconciler's window — not restarted",
                extra={"extra_fields": {"count": abandoned}},
            )
    except Exception:
        _logger.warning("Abandoned-order count failed", exc_info=True)
    while True:
        await asyncio.sleep(settings.reconcile_interval_seconds)
        try:
            with quiet():  # a poll; restarted sagas are traced by Temporal
                await reconcile_once(client)
        except Exception:
            # Never takes the workers down; retried next run.
            _logger.warning("Order reconciler run failed", exc_info=True)
