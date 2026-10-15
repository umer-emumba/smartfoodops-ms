"""analytics-service's Kafka consumer (docs/week3-plan.md W3-D9): consumer
group `analytics-service` on every event topic and every DLQ. Events update
the read model (app/facts.py); dead letters are counted as failed events.

No processed-events table: the Mongo writes are idempotent by construction.
Runs inside the API process in embedded mode (started from app.main's
lifespan); `python -m app.consumer` in standalone mode."""

from collections.abc import Callable

from confluent_kafka import Message
from sfo_events import DomainEvent, Topic, dlq_topic
from sfo_events.consumer import ConsumerLoop, run_standalone
from sfo_events.kafka import (
    EventDeserializer,
    make_consumer,
    make_producer,
    make_registry_client,
)
from sfo_observability import configure_logging

from app import facts, mongo
from app.config import settings

GROUP = settings.service_name
EVENT_TOPICS = [topic.value for topic in Topic]
DLQ_TOPICS = [dlq_topic(topic) for topic in EVENT_TOPICS]


async def process(event: DomainEvent) -> None:
    await facts.apply_event(mongo.db, event)


async def process_dead_letter(msg: Message) -> None:
    await facts.record_dead_letter(mongo.db, msg)


def build_consumer_loop() -> tuple[ConsumerLoop, Callable[[], None]]:
    """The loop and a `close` for shutdown."""
    consumer = make_consumer(settings.kafka_bootstrap_servers, GROUP)
    consumer.subscribe(EVENT_TOPICS + DLQ_TOPICS)
    dlq_producer = make_producer(settings.kafka_bootstrap_servers)
    loop = ConsumerLoop(
        consumer=consumer,
        deserialize=EventDeserializer(make_registry_client(settings.schema_registry_url)),
        process=process,
        dlq_producer=dlq_producer,
        group=GROUP,
        raw_topics=frozenset(DLQ_TOPICS),
        process_raw=process_dead_letter,
    )

    def close() -> None:
        consumer.close()
        dlq_producer.flush(5)

    return loop, close


if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    run_standalone(*build_consumer_loop())
