from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_common.temporal import embedded_workers
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app.api import internal
from app.api.routes import router
from app.config import settings
from app.db import engine
from app.redis_client import redis
from app.worker import WORKERS

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Starts this service's Temporal worker inside the API process: it picks up
    # stock, kitchen-slot and promo steps from Temporal and runs them here.
    async with embedded_workers(WORKERS, settings, service_name=settings.service_name):
        yield
    await redis.aclose()


app = create_app(
    title="SmartFoodOps — Restaurant Service",
    tags_metadata=[{"name": "restaurants", "description": "Onboarding, menus, availability"}],
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
app.include_router(internal.router)


async def _readiness() -> bool:
    return await check_db_ready(engine)


add_health_routes(app, readiness_check=_readiness)
