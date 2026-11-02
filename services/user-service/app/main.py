import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app.api import wellknown
from app.api.routes import router
from app.config import settings
from app.db import engine
from app.maintenance import run_refresh_token_purge_loop
from app.signing import get_signer

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Fails the start-up if the signing key is missing or not an Ed25519 key.
    get_signer()
    # Background job in this process: periodically deletes expired refresh
    # tokens (app/maintenance.py). Not a Temporal/Celery job — user-service has
    # neither. Cancelled on shutdown so the process can exit cleanly.
    purge_task = asyncio.create_task(run_refresh_token_purge_loop())
    yield
    purge_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await purge_task


app = create_app(
    title="SmartFoodOps — User Service",
    tags_metadata=[{"name": "auth", "description": "Registration, login, tokens, roles"}],
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
app.include_router(wellknown.router)


async def _readiness() -> bool:
    return await check_db_ready(engine)


add_health_routes(app, readiness_check=_readiness)
