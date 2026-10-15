"""Kafka plumbing shared by the outbox relays and the consumers.

- Producer: idempotent, `acks=all`, keyed by `aggregate_id` (W3-D2).
- Values: the event envelope as JSON Schema, registered in Schema Registry
  under `<topic>-<EventType>` (TopicRecordNameStrategy, W3-D3) — several
  event types share one topic.
- Consumers: manual offset commits only (W3-D4); the loop is in
  sfo_events.consumer.
"""

import json
from typing import Any

from confluent_kafka import Consumer, Message, Producer
from confluent_kafka.schema_registry import (
    Schema,
    SchemaRegistryClient,
    topic_record_subject_name_strategy,
)
from confluent_kafka.schema_registry.json_schema import JSONDeserializer, JSONSerializer
from confluent_kafka.serialization import MessageField, SerializationContext

from sfo_events.payloads import envelope_json_schema
from sfo_events.schemas import EVENT_TOPICS, DomainEvent, EventType

COMPATIBILITY = "BACKWARD"

# Message headers. Envelope fields are repeated here so tools (and the DLQ)
# can see what a message is without deserializing it.
HEADER_EVENT_ID = "event_id"
HEADER_EVENT_TYPE = "event_type"
HEADER_CORRELATION_ID = "correlation_id"
# DLQ headers (W3-D5).
HEADER_DLQ_TOPIC = "dlq.original.topic"
HEADER_DLQ_PARTITION = "dlq.original.partition"
HEADER_DLQ_OFFSET = "dlq.original.offset"
HEADER_DLQ_GROUP = "dlq.consumer.group"
HEADER_DLQ_ERROR_TYPE = "dlq.error.type"
HEADER_DLQ_ERROR = "dlq.error.message"
HEADER_DLQ_ATTEMPTS = "dlq.attempts"

Headers = list[tuple[str, bytes]]


def make_producer(bootstrap_servers: str, **overrides: Any) -> Producer:
    """No lost or duplicated writes from producer retries; per-key order
    holds even with in-flight retries (idempotence)."""
    return Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
            "linger.ms": 5,
            **overrides,
        }
    )


def make_consumer(bootstrap_servers: str, group_id: str, **overrides: Any) -> Consumer:
    """Offsets are committed by the loop only after an event's effect is
    committed (or it's dead-lettered) — never automatically."""
    return Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            **overrides,
        }
    )


def make_registry_client(url: str) -> SchemaRegistryClient:
    return SchemaRegistryClient({"url": url})


def subject_for(event_type: EventType) -> str:
    return f"{EVENT_TOPICS[event_type]}-{event_type.value}"


class EventSerializer:
    """DomainEvent → registry-framed JSON bytes, one JSONSerializer per event
    type (each has its own schema). Values are validated against the schema
    before they're sent."""

    def __init__(self, registry: SchemaRegistryClient) -> None:
        self._registry = registry
        self._serializers: dict[EventType, JSONSerializer] = {}

    def register_all(self) -> None:
        """Registers every event schema up front (relay start-up, W3-D3), so
        an incompatible change fails at deploy, not on the first event of
        that type."""
        for event_type in EventType:
            subject = subject_for(event_type)
            self._registry.set_compatibility(subject_name=subject, level=COMPATIBILITY)
            self._registry.register_schema(
                subject, Schema(json.dumps(envelope_json_schema(event_type)), "JSON")
            )

    def _serializer(self, event_type: EventType) -> JSONSerializer:
        serializer = self._serializers.get(event_type)
        if serializer is None:
            serializer = JSONSerializer(
                json.dumps(envelope_json_schema(event_type)),
                self._registry,
                conf={"subject.name.strategy": topic_record_subject_name_strategy},
            )
            self._serializers[event_type] = serializer
        return serializer

    def serialize(self, event: DomainEvent) -> bytes:
        topic = EVENT_TOPICS[event.event_type]
        ctx = SerializationContext(topic, MessageField.VALUE)
        value = self._serializer(event.event_type)(event.model_dump(mode="json"), ctx)
        assert value is not None
        return value


class EventDeserializer:
    """Registry-framed bytes → DomainEvent, validated against the schema the
    producer wrote with (looked up by the ID in the message)."""

    def __init__(self, registry: SchemaRegistryClient) -> None:
        self._deserializer = JSONDeserializer(None, schema_registry_client=registry)

    def __call__(self, msg: Message) -> dict[str, Any]:
        ctx = SerializationContext(msg.topic() or "", MessageField.VALUE, msg.headers())
        data = self._deserializer(msg.value(), ctx)
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
        return data


def event_headers(event: DomainEvent) -> Headers:
    headers = [
        (HEADER_EVENT_ID, event.event_id.encode()),
        (HEADER_EVENT_TYPE, event.event_type.value.encode()),
    ]
    if event.correlation_id:
        headers.append((HEADER_CORRELATION_ID, event.correlation_id.encode()))
    return headers


def header_value(headers: Headers | None, name: str) -> str | None:
    """Last value of header `name` (Kafka allows repeats), decoded."""
    value = None
    for key, raw in headers or []:
        if key == name and raw is not None:
            value = raw.decode(errors="replace")
    return value


def dlq_headers(msg: Message, *, group: str, error: BaseException, attempts: int) -> Headers:
    """The original headers plus where the message came from and why it
    failed."""
    return [
        *(msg.headers() or []),
        (HEADER_DLQ_TOPIC, (msg.topic() or "").encode()),
        (HEADER_DLQ_PARTITION, str(msg.partition()).encode()),
        (HEADER_DLQ_OFFSET, str(msg.offset()).encode()),
        (HEADER_DLQ_GROUP, group.encode()),
        (HEADER_DLQ_ERROR_TYPE, type(error).__name__.encode()),
        (HEADER_DLQ_ERROR, str(error)[:1000].encode()),
        (HEADER_DLQ_ATTEMPTS, str(attempts).encode()),
    ]
