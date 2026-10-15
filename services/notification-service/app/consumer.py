"""notification-service's Kafka consumer (docs/week3-plan.md W3-D4, W3-D7):
every topic, consumer group `notification-service`. For each event it
writes one `pending` notification per recipient and channel
(app/templates.py) together with the `processed_events` row, and once that
is committed, enqueues the sends on the Celery worker.

Runs inside the API process in embedded mode (started from app.main's
lifespan); `python -m app.consumer` in standalone mode."""

import asyncio
from collections.abc import Callable

from sfo_common import new_id
from sfo_events import AfterCommit, DomainEvent, Topic, idempotent
from sfo_events.consumer import ConsumerLoop, run_standalone
from sfo_events.kafka import (
    EventDeserializer,
    make_consumer,
    make_producer,
    make_registry_client,
)
from sfo_observability import configure_logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionFactory
from app.models import Notification, ProcessedEvent
from app.templates import messages_for

GROUP = settings.service_name


async def handle(session: AsyncSession, event: DomainEvent) -> AfterCommit | None:
    rows = [
        Notification(
            id=new_id(),
            user_id=message.user_id,
            recipient_role=message.recipient_role,
            event_id=event.event_id,
            related_order_id=message.order_id,
            type=message.type,
            channel=channel,
            content=message.content,
        )
        for message in messages_for(event)
        for channel in message.channels
    ]
    if not rows:
        return None
    session.add_all(rows)
    ids = [row.id for row in rows]

    async def enqueue() -> None:
        # Imported here: the Celery app needs the broker URL, which only
        # processes that run the consumer are given.
        from app.celery_app import enqueue_sends

        await asyncio.to_thread(enqueue_sends, ids)

    return enqueue


def build_consumer_loop() -> tuple[ConsumerLoop, Callable[[], None]]:
    """The loop and a `close` for shutdown."""
    consumer = make_consumer(settings.kafka_bootstrap_servers, GROUP)
    consumer.subscribe([topic.value for topic in Topic])
    dlq_producer = make_producer(settings.kafka_bootstrap_servers)
    loop = ConsumerLoop(
        consumer=consumer,
        deserialize=EventDeserializer(make_registry_client(settings.schema_registry_url)),
        process=idempotent(SessionFactory, ProcessedEvent, GROUP, handle),
        dlq_producer=dlq_producer,
        group=GROUP,
    )

    def close() -> None:
        consumer.close()
        dlq_producer.flush(5)

    return loop, close


if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    run_standalone(*build_consumer_loop())
