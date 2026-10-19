"""OpenTelemetry tracing (docs/week3-plan.md W3-D11, W3-D12).

`setup_tracing(service_name)` runs once per process, from each service's
`app/__init__.py` — before any module creates a client, since PyMongo's
instrumentation only sees clients created after it. Tracing is on only
when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (docker compose sets it); without
it (unit tests, a bare `uvicorn`) nothing is exported. Spans go over OTLP/HTTP
straight to Jaeger, which is itself an OTel Collector distribution (W3-D11).
The sampler comes from the standard `OTEL_TRACES_SAMPLER`/`_ARG` variables.

What becomes a span on its own: FastAPI requests (`instrument_fastapi`),
outgoing httpx calls (with `traceparent` added, so the called service joins
the trace), SQL (`instrument_engine`), Redis, MongoDB, and Celery tasks where
wanted (`instrument_celery`).
Temporal (sfo_common.temporal) and Kafka (sfo_events) add their own.

Requests that run every few seconds and mean nothing — health probes,
Prometheus scrapes — are excluded, along with the SQL/Redis they do
(`quiet`); so is the relay's 1s poll (sfo_events.relay). Otherwise each
container would start a new trace every few seconds and bury the real ones.
Long-lived SSE streams aren't traced either: a span that stays open for the
whole stream helps nobody."""

import contextlib
import os
from collections.abc import Iterator
from importlib.util import find_spec
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.types import ASGIApp, Receive, Scope, Send

_configured = False

# Matched as regexes against the URL by the FastAPI instrumentation.
QUIET_PATHS = ("/health", "/livez", "/readyz", "/metrics")
_UNTRACED_URLS = ",".join([*QUIET_PATHS, "/v1/tracking/"])


def tracing_enabled() -> bool:
    """On only when docker compose sets where to send spans (Jaeger); off in
    unit tests, so they never try to reach Jaeger."""
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def setup_tracing(service_name: str) -> None:
    """Turns tracing on for this process, once: names it (as shown in Jaeger),
    sends its spans to Jaeger in the background, and auto-traces outgoing HTTP
    calls, Redis and MongoDB. Without it no spans are recorded at all."""
    global _configured
    if _configured:
        return
    _configured = True
    if not tracing_enabled():
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # OTEL_SERVICE_NAME, if set, wins: a worker running the service's image
    # (e.g. order-worker) shows up in Jaeger under its own name.
    name = os.environ.get("OTEL_SERVICE_NAME") or service_name
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # Only the client libraries this service has installed (each image has
    # only its own dependencies; the instrumentations import their library).
    if find_spec("httpx"):
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    if find_spec("redis"):
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    if find_spec("pymongo"):
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor

        PymongoInstrumentor().instrument()


def instrument_celery() -> None:
    """Spans for enqueueing and running Celery tasks, with the trace carried
    in the task message — for notification sending. Not for the outbox
    relays: a task span every second would bury everything else (their
    publishes are traced by sfo_events instead)."""
    if not tracing_enabled():
        return
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    CeleryInstrumentor().instrument()


def instrument_fastapi(app: Any) -> None:
    """A server span per request (except the quiet paths and SSE streams),
    continuing the caller's trace from its `traceparent` header."""
    app.add_middleware(_QuietPaths)
    if not tracing_enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=_UNTRACED_URLS)


def instrument_engine(engine: Any) -> None:
    """SQL spans for an async SQLAlchemy engine."""
    if not tracing_enabled():
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """No spans from instrumented libraries inside — for background polling
    that would otherwise start a trace every tick."""
    with suppress_instrumentation():
        yield


class _QuietPaths:
    """Health probes and metrics scrapes: no spans for their database or
    Redis checks either (the request itself is excluded above)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(QUIET_PATHS):
            with quiet():
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)


def current_trace_ids() -> tuple[str | None, str | None]:
    """The active span's trace and span IDs as hex, for log lines."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")
