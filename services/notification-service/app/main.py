from sfo_common import create_app
from sfo_common.db import check_db_ready
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging

from app.api.routes import router
from app.config import settings
from app.db import engine

configure_logging(settings.service_name, settings.log_level)

app = create_app(
    title="SmartFoodOps — Notification Service",
    tags_metadata=[{"name": "notifications", "description": "Order/delivery notifications (Kafka consumer added Week 3)"}],
)

app.include_router(router)


async def _readiness() -> bool:
    return await check_db_ready(engine)


add_health_routes(app, readiness_check=_readiness)
