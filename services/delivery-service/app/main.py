from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging

from app.api.routes import router
from app.config import settings
from app.db import engine
from app.mongo import check_mongo_ready, ensure_indexes

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await ensure_indexes()
    yield


app = create_app(
    title="SmartFoodOps — Delivery Service",
    tags_metadata=[
        {
            "name": "riders",
            "description": "Rider registration/availability (assignment added Week 2)",
        }
    ],
)
app.router.lifespan_context = _lifespan

app.include_router(router)


async def _readiness() -> bool:
    return await check_db_ready(engine) and await check_mongo_ready()


add_health_routes(app, readiness_check=_readiness)
