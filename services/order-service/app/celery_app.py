"""order-service's Celery worker (docs/week3-plan.md W3-D1): the outbox relay to
Kafka every second, and the purge of old dispatched rows. Same image as the
API; run as the `order-worker` container (command in docker-compose.yml)."""

from sfo_common.celery import make_celery_app
from sfo_events.relay import OutboxRelay, register_relay_tasks
from sfo_observability import configure_logging, instrument_celery_metrics

from app.config import settings
from app.db import SessionFactory
from app.models import OutboxEvent

app = make_celery_app(
    settings.service_name,
    settings.celery_broker_url,
    configure_logging=lambda: configure_logging(settings.service_name, settings.log_level),
)
# Task outcomes and durations; serves the worker's metrics port.
instrument_celery_metrics()
register_relay_tasks(
    app,
    OutboxRelay(
        session_factory=SessionFactory,
        outbox_model=OutboxEvent,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        schema_registry_url=settings.schema_registry_url,
        batch_size=settings.outbox_relay_batch_size,
    ),
    interval_seconds=settings.outbox_relay_interval_seconds,
    retention_days=settings.outbox_retention_days,
)
