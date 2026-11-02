from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app import qdrant
from app.config import settings
from app.db import engine

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    yield
    await qdrant.client.close()


app = create_app(
    title="SmartFoodOps — AI Service",
    tags_metadata=[
        {
            "name": "ai",
            "description": "Semantic search over menus, restaurants, reviews and order history",
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


async def _readiness() -> bool:
    """Ready only when both stores answer: search needs Qdrant, and every
    write path needs ai_db."""
    return await check_db_ready(engine) and await qdrant.check_ready()


add_health_routes(app, readiness_check=_readiness)
