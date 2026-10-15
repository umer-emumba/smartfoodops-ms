"""notification-service's Celery app (docs/week3-plan.md W3-D7). The worker
(`notification-worker` container, command in docker-compose.yml) runs the
tasks in app/tasks.py. The API process only enqueues — by task name, via
`enqueue_sends` — and never imports the tasks."""

from sfo_common.celery import make_celery_app
from sfo_observability import configure_logging, instrument_celery, instrument_celery_metrics

from app.config import settings

SEND_NOTIFICATION = f"{settings.service_name}.send_notification"
SWEEP_PENDING = f"{settings.service_name}.sweep_pending"

app = make_celery_app(
    settings.service_name,
    settings.celery_broker_url,
    configure_logging=lambda: configure_logging(settings.service_name, settings.log_level),
)
# Task outcomes and durations; serves the worker's metrics port.
instrument_celery_metrics()
app.conf.include = ["app.tasks"]
# Enqueue (API process) → send (worker) in the event's trace.
instrument_celery()
app.conf.beat_schedule = {
    "sweep-pending": {
        "task": SWEEP_PENDING,
        "schedule": settings.notify_sweep_interval_seconds,
        "options": {"expires": settings.notify_sweep_interval_seconds},
    }
}


def enqueue_sends(notification_ids: list[str]) -> None:
    """Blocking (publishes to RabbitMQ) — call from a thread in async code."""
    for notification_id in notification_ids:
        app.send_task(SEND_NOTIFICATION, args=[notification_id])
