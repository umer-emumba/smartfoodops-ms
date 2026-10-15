"""Sending, retries and the sweep (app/tasks.py, week3-plan W3-D7) against
the real notification database."""

from datetime import UTC, datetime, timedelta

import pytest
from app import tasks
from app.config import settings
from app.models import Notification
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]


async def _add(
    factory: Factory, channel: str, *, status: str = "pending", age: timedelta = timedelta(0)
) -> str:
    notification = Notification(
        user_id="u-1",
        recipient_role="customer",
        event_id=f"e-{channel}-{status}-{age.total_seconds()}",
        type="order_update",
        channel=channel,
        content="hello",
        status=status,
        created_at=datetime.now(UTC) - age,
    )
    async with factory() as session:
        session.add(notification)
        await session.commit()
    return notification.id


async def _get(factory: Factory, notification_id: str) -> Notification:
    async with factory() as session:
        notification = await session.get(Notification, notification_id)
        assert notification is not None
        return notification


async def test_in_app_is_sent_at_once(session_factory: Factory) -> None:
    notification_id = await _add(session_factory, "in_app")
    assert await tasks.deliver(notification_id) is None
    row = await _get(session_factory, notification_id)
    assert row.status == "sent" and row.sent_at is not None and row.retry_count == 0


async def test_failing_email_backs_off_then_fails_for_good(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "notify_fake_failure_rate", 1.0)
    notification_id = await _add(session_factory, "email")

    delays = [await tasks.deliver(notification_id) for _ in range(settings.notify_max_attempts)]

    # 2s, 4s, 8s, 16s (±20% jitter), then no more.
    assert delays[-1] is None
    for delay, base in zip(delays[:-1], (2, 4, 8, 16), strict=True):
        assert delay is not None and base * 0.8 <= delay <= base * 1.2
    row = await _get(session_factory, notification_id)
    assert (row.status, row.retry_count) == ("failed", settings.notify_max_attempts)
    assert row.last_error == "simulated email provider failure"
    # A late duplicate task changes nothing.
    assert await tasks.deliver(notification_id) is None
    row = await _get(session_factory, notification_id)
    assert row.retry_count == settings.notify_max_attempts


async def test_email_recovers_after_a_failure(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    notification_id = await _add(session_factory, "email")
    monkeypatch.setattr(settings, "notify_fake_failure_rate", 1.0)
    assert await tasks.deliver(notification_id) is not None
    monkeypatch.setattr(settings, "notify_fake_failure_rate", 0.0)
    assert await tasks.deliver(notification_id) is None
    row = await _get(session_factory, notification_id)
    assert (row.status, row.retry_count) == ("sent", 1)


async def test_a_row_being_sent_is_skipped_by_a_duplicate_task(session_factory: Factory) -> None:
    notification_id = await _add(session_factory, "sms")
    async with session_factory() as other_worker:
        await other_worker.execute(
            select(Notification).where(Notification.id == notification_id).with_for_update()
        )
        assert await tasks.deliver(notification_id) is None
    assert (await _get(session_factory, notification_id)).status == "pending"


async def test_sweep_finds_only_stale_pending_rows(session_factory: Factory) -> None:
    stale = await _add(session_factory, "email", age=timedelta(minutes=5))
    await _add(session_factory, "email", age=timedelta(seconds=5))
    await _add(session_factory, "email", status="sent", age=timedelta(minutes=5))
    assert await tasks.stale_pending_ids(timedelta(seconds=60)) == [stale]
