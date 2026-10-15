from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_common.temporal import embedded_workers
from sfo_observability import configure_logging, instrument_fastapi, instrument_metrics

from app import restaurant_client
from app.api import internal
from app.api.routes import router
from app.config import settings
from app.db import engine
from app.redis_client import redis
from app.worker import BACKGROUND, WORKERS

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with embedded_workers(
        WORKERS, settings, service_name=settings.service_name, background=BACKGROUND
    ):
        yield
    await restaurant_client.client.aclose()
    await redis.aclose()


app = create_app(
    title="SmartFoodOps — Order Service",
    tags_metadata=[{"name": "orders", "description": "Order lifecycle, driven by the order saga"}],
    lifespan=_lifespan,
)

instrument_fastapi(app)
instrument_metrics(app)
app.include_router(router)
app.include_router(internal.router)


async def _readiness() -> bool:
    return await check_db_ready(engine)


add_health_routes(app, readiness_check=_readiness)
