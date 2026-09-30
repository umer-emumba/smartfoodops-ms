from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sfo_common.errors import register_exception_handlers
from sfo_common.middleware import CorrelationIdMiddleware


def create_app(
    *,
    title: str,
    version: str = "0.1.0",
    tags_metadata: list[dict] | None = None,
    enable_cors: bool = False,
    cors_origins: list[str] | None = None,
    docs_url: str | None = "/docs",
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Shared FastAPI app factory so every service's Swagger UI looks/organizes
    the same way (docs/architecture.md#api-documentation), and every service
    gets the same error envelope + correlation-ID propagation for free.

    `enable_cors` is only ever True for api-gateway — CORS is handled once, at
    the edge (docs/architecture.md#conventions); downstream services aren't
    browser-facing.

    `docs_url=None` lets api-gateway serve its own cross-service docs index
    at /docs instead of FastAPI's built-in Swagger page, which would
    otherwise be registered first and shadow it.
    """
    app = FastAPI(
        title=title,
        version=version,
        openapi_tags=tags_metadata,
        docs_url=docs_url,
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(CorrelationIdMiddleware)

    if enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins or [],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    return app
