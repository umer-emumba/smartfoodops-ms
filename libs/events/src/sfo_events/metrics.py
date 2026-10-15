"""Prometheus metrics for the event pipeline (docs/week3-plan.md step 5),
served by each process's metrics server (sfo_observability.metrics).

The outbox metrics come from a relay worker (one relay per service, so the
scrape target says which service); the consumer metrics carry the consumer
group."""

from prometheus_client import Counter, Gauge

OUTBOX_PUBLISHED = Counter("outbox_published_total", "Outbox rows published to Kafka")
OUTBOX_PUBLISH_FAILURES = Counter(
    "outbox_publish_failures_total", "Outbox rows that failed to publish (retried next tick)"
)
OUTBOX_BACKLOG = Gauge("outbox_backlog", "Outbox rows not yet published")
OUTBOX_OLDEST_AGE = Gauge(
    "outbox_oldest_age_seconds", "Age of the oldest unpublished outbox row (0 if none)"
)

EVENTS_CONSUMED = Counter(
    "kafka_events_consumed_total",
    "Messages a consumer finished with, by outcome",
    ["group", "topic", "outcome"],  # processed | skipped | dead_lettered
)
EVENTS_DUPLICATE = Counter(
    "kafka_events_duplicates_total",
    "Redelivered events a consumer had already applied (a no-op; counted as processed too)",
    ["group"],
)
EVENT_RETRIES = Counter(
    "kafka_event_retries_total",
    "Retries before a message was finished",
    ["group", "topic", "kind"],  # handler: the handler failed; outage: a dependency was down
)
CONSUMER_LAG = Gauge(
    "kafka_consumer_lag",
    "Messages behind the end of the partition",
    ["group", "topic", "partition"],
)
