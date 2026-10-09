from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_common.temporal import embedded_workers
from sfo_observability import configure_logging

from app import mongo
from app.api.routes import router
from app.config import settings
from app.db import engine
from app.worker import WORKERS

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await mongo.ensure_indexes()
    async with embedded_workers(WORKERS, settings, service_name=settings.service_name):
        yield
    await mongo.close()


app = create_app(
    title="SmartFoodOps — Delivery Service",
    tags_metadata=[
        {
            "name": "riders",
            "description": "Rider registration/availability (assignment added Week 2)",
        }
    ],
    lifespan=_lifespan,
)

app.include_router(router)


async def _readiness() -> bool:
    return await check_db_ready(engine) and await mongo.check_mongo_ready()


add_health_routes(app, readiness_check=_readiness)
