from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging

from app import restaurant_client
from app.api.routes import router
from app.config import settings
from app.db import engine

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await restaurant_client.client.aclose()


app = create_app(
    title="SmartFoodOps — Order Service",
    tags_metadata=[{"name": "orders", "description": "Order lifecycle (saga added Week 2)"}],
    lifespan=_lifespan,
)

app.include_router(router)


async def _readiness() -> bool:
    return await check_db_ready(engine)


add_health_routes(app, readiness_check=_readiness)
