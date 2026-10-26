"""Sending notifications (docs/week3-plan.md W3-D7), on the
`notification-worker`.

`send_notification` takes the row with `FOR UPDATE SKIP LOCKED` and holds
it while sending: a duplicate task for the same row (the sweep, a
redelivery) skips it, and a worker killed mid-send releases the lock with
its connection, so the redelivered task (`acks_late`) can send it.
Attempts are counted in the row (`retry_count`), not in Celery, so the
limit holds however many task messages exist for it. The send is
at-least-once: a crash after the provider accepted but before the commit
sends again.

`sweep_pending` re-enqueues pending rows nobody has tried for a while — the
consumer commits the rows and then enqueues, and a crash or RabbitMQ outage
in between would otherwise strand them."""

import logging
import random
from datetime import UTC, datetime, timedelta

from celery import Task
from prometheus_client import Counter
from sfo_common.celery import run_async
from sqlalchemy import func, select

from app import senders
from app.celery_app import SEND_NOTIFICATION, SWEEP_PENDING, app
from app.config import settings
from app.db import SessionFactory
from app.models import Notification

logger = logging.getLogger(__name__)

# Each send attempt's result, by channel: sent, retry (will be tried again)
# or failed (gave up after notify_max_attempts).
NOTIFICATION_SENDS = Counter(
    "notification_sends_total", "Notification send attempts", ["channel", "result"]
)
# Every series from the start, at 0, so the first failure is an increase the
# NotificationsFailing alert can see (a series born at 1 isn't).
for _channel in ("email", "sms"):
    for _result in ("sent", "retry", "failed"):
        NOTIFICATION_SENDS.labels(_channel, _result)
NOTIFICATION_SENDS.labels("in_app", "sent")

_SWEEP_BATCH = 500


def retry_delay(failures: int) -> float:
    """Exponential (2s, 4s, 8s, ...), capped, with ±20% jitter so a burst of
    failures doesn't retry in lockstep."""
    base = settings.notify_retry_backoff_seconds * 2 ** (failures - 1)
    return min(settings.notify_retry_backoff_max_seconds, base) * random.uniform(0.8, 1.2)


async def deliver(notification_id: str) -> float | None:
    """One send attempt. Returns the delay before the next attempt, or None
    when there's nothing more to do (sent, failed for good, or not ours)."""
    async with SessionFactory() as session:
        notification = (
            await session.execute(
                select(Notification)
                .where(Notification.id == notification_id, Notification.status == "pending")
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if notification is None:
            return None
        now = datetime.now(UTC)
        notification.last_attempt_at = now
        delay = None
        try:
            await senders.send(notification)
        except senders.SendError as exc:
            notification.retry_count += 1
            notification.last_error = str(exc)[:1000]
            if notification.retry_count >= settings.notify_max_attempts:
                notification.status = "failed"
                NOTIFICATION_SENDS.labels(notification.channel, "failed").inc()
                logger.error(
                    "Notification %s (%s) failed after %d attempts: %s",
                    notification.id,
                    notification.channel,
                    notification.retry_count,
                    exc,
                )
            else:
                delay = retry_delay(notification.retry_count)
                NOTIFICATION_SENDS.labels(notification.channel, "retry").inc()
        else:
            notification.status = "sent"
            notification.sent_at = now
            NOTIFICATION_SENDS.labels(notification.channel, "sent").inc()
        await session.commit()
        return delay


async def stale_pending_ids(min_age: timedelta) -> list[str]:
    cutoff = datetime.now(UTC) - min_age
    async with SessionFactory() as session:
        rows = await session.execute(
            select(Notification.id)
            .where(
                Notification.status == "pending",
                func.coalesce(Notification.last_attempt_at, Notification.created_at) < cutoff,
            )
            .order_by(Notification.created_at)
            .limit(_SWEEP_BATCH)
        )
        return list(rows.scalars().all())


@app.task(  # type: ignore[untyped-decorator]
    name=SEND_NOTIFICATION, bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=None
)
def send_notification(self: Task, notification_id: str) -> None:
    delay = run_async(deliver(notification_id))
    if delay is not None:
        raise self.retry(countdown=delay)


@app.task(name=SWEEP_PENDING)  # type: ignore[untyped-decorator]
def sweep_pending() -> None:
    ids = run_async(stale_pending_ids(timedelta(seconds=settings.notify_sweep_min_age_seconds)))
    for notification_id in ids:
        # Queued again for this worker to send.
        send_notification.delay(notification_id)
    if ids:
        logger.warning("Re-enqueued %d stale pending notifications", len(ids))
