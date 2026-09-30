from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi_limiter import FastAPILimiter
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging

from app import auth_routes, docs, proxy
from app.config import settings
from app.redis_client import redis
from app.security import DenyListGuardMiddleware

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await FastAPILimiter.init(redis)
    yield


app = create_app(
    title="SmartFoodOps — API Gateway",
    enable_cors=True,
    cors_origins=settings.cors_origin_list,
)
app.router.lifespan_context = _lifespan

app.add_middleware(DenyListGuardMiddleware)

# Order matters: the specific /v1/auth/login and /v1/auth/register routes
# (stricter rate limit) must be registered before the generic /v1/{resource}
# catch-all, or the catch-all would shadow them.
app.include_router(auth_routes.router)
app.include_router(docs.router)
app.include_router(proxy.router)


async def _readiness() -> bool:
    try:
        return await redis.ping()
    except Exception:
        return False


add_health_routes(app, readiness_check=_readiness)
