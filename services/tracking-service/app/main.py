from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi

from app import state
from app.api.routes import router
from app.config import settings
from app.hub import hub
from app.redis_client import redis

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
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

instrument_fastapi(app)
app.include_router(router)


async def _readiness() -> bool:
    try:
        return await redis.ping()
    except Exception:
        return False


add_health_routes(app, readiness_check=_readiness)
