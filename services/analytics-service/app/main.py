from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app import mongo
from app.api.routes import router
from app.config import settings

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Indexes, then — in embedded mode (week3-plan W3-D6) — the Kafka
    consumer in this process. Standalone: `python -m app.consumer`."""
    await mongo.ensure_indexes()
    if settings.kafka_consumer_mode != "embedded":
        yield
    else:
        from sfo_events.consumer import running_in_background

        from app.consumer import build_consumer_loop

        async with running_in_background(*build_consumer_loop()):
            yield
    await mongo.client.close()


app = create_app(
    title="SmartFoodOps — Analytics Service",
    tags_metadata=[
        {
            "name": "analytics",
            "description": "Order, delivery and rider metrics, built from domain events",
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

add_health_routes(app, readiness_check=mongo.check_ready)
