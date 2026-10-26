from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app import state
from app.api.routes import router
from app.config import settings
from app.hub import hub
from app.redis_client import redis

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Embedded mode: the Kafka consumer that feeds the live streams
    (app/consumer.py) runs in this process. Standalone: `python -m
    app.consumer` runs it instead."""
    if settings.kafka_consumer_mode == "embedded":
        from sfo_events.consumer import running_in_background

        from app.consumer import build_consumer_loop

        async with running_in_background(*build_consumer_loop()):
            yield
    else:
        yield
    await hub.close()
    await state.close()
    await redis.aclose()


app = create_app(
    title="SmartFoodOps — Tracking Service",
    tags_metadata=[
        {
            "name": "tracking",
            "description": "Live order tracking and restaurant new-order feed (SSE)",
        }
    ],
    lifespan=_lifespan,
)

# Tracing: every request becomes a span in its trace (sent to Jaeger), and an
# incoming `traceparent` header joins the caller's trace. This is what links
# one order across all services. Health checks and metrics scrapes are skipped.
instrument_fastapi(app)
# Metrics: counts every request by route and status code, and times it.
# Prometheus reads them from this process's port 9100 every 15s; they feed the
# Grafana "Service overview" dashboard and the HighErrorRate alert.
instrument_metrics(app)
app.include_router(router)


async def _readiness() -> bool:
    try:
        return await redis.ping()
    except Exception:
        return False


add_health_routes(app, readiness_check=_readiness)
