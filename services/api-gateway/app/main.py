from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sfo_common import create_app
from sfo_common.health import add_health_routes
from sfo_observability import configure_logging, instrument_fastapi

from app import auth_routes, docs, proxy
from app.config import settings
from app.query_token import QueryTokenMiddleware
from app.redis_client import redis

configure_logging(settings.service_name, settings.log_level)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    for client in proxy.clients.values():
        await client.aclose()
    await redis.aclose()


app = create_app(
    title="SmartFoodOps — API Gateway",
    enable_cors=True,
    cors_origins=settings.cors_origin_list,
    # /docs is the gateway's own cross-service index (app/docs.py), not
    # FastAPI's built-in Swagger page for the gateway's single catch-all route.
    docs_url=None,
    lifespan=_lifespan,
)

# Order matters: the specific /v1/auth/* routes (stricter rate limit) must be
# registered before the generic /v1/{resource} catch-all, or the catch-all
# would shadow them. Auth (JWT + deny-list) runs inside forward_request, not
# as middleware, so its errors get the normal JSON envelope.
# Outermost: the token must be in the Authorization header before the rate
# limiter and auth run (app/query_token.py).
app.add_middleware(QueryTokenMiddleware)

instrument_fastapi(app)
app.include_router(auth_routes.router)
app.include_router(docs.router)
app.include_router(proxy.router)


async def _readiness() -> bool:
    try:
        return await redis.ping()
    except Exception:
        return False


add_health_routes(app, readiness_check=_readiness)
