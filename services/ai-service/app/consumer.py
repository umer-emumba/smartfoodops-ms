"""ai-service's Kafka consumer (docs/week4-plan.md step 3): consumer group
`ai-indexer` on restaurant-events and order-events. Each event becomes index
writes (app/indexer.py).

No processed-events table: the writes are idempotent by version
(app/store.py), so a redelivered event changes nothing. A Qdrant outage is
retried until Qdrant is back, never dead-lettered (QdrantUnavailable).

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

from app.config import settings
from app.indexer import Indexer
from app.store import default_store

GROUP = "ai-indexer"


def build_consumer_loop() -> tuple[ConsumerLoop, Callable[[], None]]:
    """The loop and a `close` for shutdown."""
    indexer = Indexer(default_store())

    async def process(event: DomainEvent) -> None:
        await indexer.apply(event)

    # From the oldest retained events: a new index catches up on its own
    # (`python -m app.reindex` covers what's older than Kafka keeps).
    consumer = make_consumer(settings.kafka_bootstrap_servers, GROUP)
    consumer.subscribe([Topic.RESTAURANT.value, Topic.ORDER.value])
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
