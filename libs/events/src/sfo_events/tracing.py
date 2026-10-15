"""Carrying a trace across the outbox and Kafka (docs/week3-plan.md W3-D12).

The writer's `traceparent` is stored on the outbox row (`trace_context`);
the relay's publish span continues that trace and puts its own
`traceparent` in the Kafka headers; the consumer's span continues from
there. So one order's trace runs from the HTTP request, through the saga,
across Kafka to every consumer. All of it is a no-op while tracing is off."""

import contextlib
from collections.abc import Iterator

from opentelemetry import context, propagate, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import Span, SpanKind

tracer = trace.get_tracer("sfo_events")

# Kafka message headers as confluent-kafka types them (no import of it here:
# the outbox uses this module, and services that only write outbox rows
# shouldn't load the Kafka client).
HeadersLike = list[tuple[str, str | bytes | None]] | dict[str, str | bytes | None] | None


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def _from_traceparent(traceparent: str | None) -> context.Context:
    return propagate.extract({"traceparent": traceparent} if traceparent else {})


def _from_headers(headers: HeadersLike) -> context.Context:
    carrier = {
        key: value if isinstance(value, str) else value.decode(errors="replace")
        for key, value in (headers.items() if isinstance(headers, dict) else headers or [])
        if value is not None
    }
    return propagate.extract(carrier)


@contextlib.contextmanager
def publish_span(
    topic: str, event_type: str, event_id: str, traceparent: str | None
) -> Iterator[Span]:
    """The relay publishing one outbox row, inside the trace that wrote it.
    Not suppressed by `quiet()` — a manual span, unlike the poll's SQL."""
    with tracer.start_as_current_span(
        f"{topic} publish",
        context=_from_traceparent(traceparent),
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": topic,
            "messaging.message.id": event_id,
            "event_type": event_type,
        },
    ) as span:
        yield span


@contextlib.contextmanager
def process_span(
    topic: str,
    headers: HeadersLike,
    group: str,
    event_type: str | None = None,
) -> Iterator[Span]:
    """A consumer handling one message, continuing the producer's trace."""
    attributes = {
        "messaging.system": "kafka",
        "messaging.destination.name": topic,
        "messaging.consumer.group.name": group,
    }
    if event_type:
        attributes["event_type"] = event_type
    with tracer.start_as_current_span(
        f"{topic} process",
        context=_from_headers(headers),
        kind=SpanKind.CONSUMER,
        attributes=attributes,
    ) as span:
        yield span


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """No spans from instrumented libraries (SQL, HTTP) inside."""
    with suppress_instrumentation():
        yield
