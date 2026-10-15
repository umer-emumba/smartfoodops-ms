from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi

from app import mongo
from app.api.routes import router
from app.config import settings

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
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

instrument_fastapi(app)
app.include_router(router)
# Prometheus scrapes this on the container network; api-gateway only proxies
# /v1/{resource}, so it isn't reachable from outside.
app.mount("/metrics", make_asgi_app())

add_health_routes(app, readiness_check=mongo.check_ready)
