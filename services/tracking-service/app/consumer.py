"""tracking-service's Kafka consumer: consumer group `tracking-service` on
order-events. Each order event updates the live-feed state and publishes
the change to the open streams (app/projection.py).

No processed-events table: applying an event is idempotent by
construction (never backwards, app/projection.py).

Runs inside the API process in embedded mode (started from app.main's
lifespan); `python -m app.consumer` in standalone mode."""

from collections.abc import Callable

from sfo_events import DomainEvent, Topic
from sfo_events.consumer import ConsumerLoop, run_standalone
from sfo_events.kafka import (
    EventDeserializer,
    make_consumer,
    make_producer,
    make_registry_client,
)
from sfo_observability import configure_logging

from app import projection, redis_client
from app.config import settings

GROUP = settings.service_name


async def process(event: DomainEvent) -> None:
    await projection.apply_order_event(redis_client.redis, event)


def build_consumer_loop() -> tuple[ConsumerLoop, Callable[[], None]]:
    """The loop and a `close` for shutdown."""
    # Starts at the oldest events like the other groups, so nothing is ever
    # skipped — events older than their state's lifetime are passed over
    # cheaply (app/projection.py).
    consumer = make_consumer(settings.kafka_bootstrap_servers, GROUP)
    consumer.subscribe([Topic.ORDER.value])
    dlq_producer = make_producer(settings.kafka_bootstrap_servers)
    loop = ConsumerLoop(
        consumer=consumer,
        deserialize=EventDeserializer(make_registry_client(settings.schema_registry_url)),
        process=process,
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
