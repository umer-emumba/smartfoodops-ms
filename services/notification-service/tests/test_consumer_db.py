"""The consumer's handler behind the idempotency wrapper, against the real
notification database (week3-plan W3-D4, W3-D7)."""

import pytest
from app.consumer import GROUP, handle
from app.models import Notification, ProcessedEvent
from prometheus_client import REGISTRY
from sfo_events import EventType, idempotent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.events import CUSTOMER, OWNER, event, order_payload, placed

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


def _duplicates() -> float:
    labels = {"group": GROUP}
    return REGISTRY.get_sample_value("kafka_events_duplicates_total", labels) or 0.0


async def _rows(factory: Factory) -> list[Notification]:
    async with factory() as session:
        result = await session.execute(select(Notification).order_by(Notification.user_id))
        return list(result.scalars().all())


async def test_one_pending_row_per_recipient_and_channel(
    session_factory: Factory, enqueued: list[str]
) -> None:
    process = idempotent(session_factory, ProcessedEvent, GROUP, handle)
    ev = event(EventType.ORDER_CANCELLED, order_payload(from_status="confirmed"))
    await process(ev)

    rows = await _rows(session_factory)
    assert sorted((r.user_id, r.recipient_role, r.channel) for r in rows) == [
        (CUSTOMER, "customer", "email"),
        (CUSTOMER, "customer", "in_app"),
        (OWNER, "restaurant_admin", "in_app"),
        (OWNER, "restaurant_admin", "sms"),
    ]
    assert {(r.status, r.event_id, r.related_order_id) for r in rows} == {
        ("pending", ev.event_id, "o-1")
    }
    # Enqueued after the commit, all of them.
    assert sorted(enqueued) == sorted(r.id for r in rows)


async def test_replayed_event_creates_no_duplicates(
    session_factory: Factory, enqueued: list[str]
) -> None:
    process = idempotent(session_factory, ProcessedEvent, GROUP, handle)
    ev = placed()
    await process(ev)
    before = _duplicates()
    await process(ev)  # offsets reset, or redelivery after a crash

    assert len(await _rows(session_factory)) == 2  # in-app + email, once
    assert len(enqueued) == 2
    assert _duplicates() == before + 1  # visible on the dashboard
    async with session_factory() as session:
        processed = await session.get(ProcessedEvent, ev.event_id)
    assert processed is not None and processed.consumer == GROUP


async def test_events_nobody_is_told_about_are_still_marked_processed(
    session_factory: Factory, enqueued: list[str]
) -> None:
    process = idempotent(session_factory, ProcessedEvent, GROUP, handle)
    ev = event(EventType.ORDER_AWAITING_RESTAURANT, order_payload(restaurant_admin_id=None))
    await process(ev)
    assert await _rows(session_factory) == [] and enqueued == []
    async with session_factory() as session:
        assert await session.get(ProcessedEvent, ev.event_id) is not None
