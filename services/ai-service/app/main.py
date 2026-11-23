import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app import qdrant
from app.api.routes import router
from app.config import settings
from app.db import engine
from app.embedding import embedder

logger = logging.getLogger(__name__)

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Loads the embedding models in the background, so the first search
    doesn't pay for it. Embedded mode (week3-plan W3-D6): the indexer's Kafka
    consumer runs in this process. Standalone: `python -m app.consumer`."""
    warm_up = asyncio.create_task(_warm_up())
    if settings.kafka_consumer_mode != "embedded":
        yield
    else:
        from sfo_events.consumer import running_in_background

        from app.consumer import build_consumer_loop

        async with running_in_background(*build_consumer_loop()):
            yield
    warm_up.cancel()
    await qdrant.client.close()


async def _warm_up() -> None:
    try:
        await embedder.query("warm up")
    except Exception:
        logger.warning("Embedding model warm-up failed", exc_info=True)


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
app.include_router(router)


async def _readiness() -> bool:
    """Ready only when both stores answer: search needs Qdrant, and every
    write path needs ai_db."""
    return await check_db_ready(engine) and await qdrant.check_ready()


add_health_routes(app, readiness_check=_readiness)
