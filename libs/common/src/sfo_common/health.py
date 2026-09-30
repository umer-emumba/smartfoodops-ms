from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[bool]]


def add_health_routes(app: FastAPI, readiness_check: ReadinessCheck | None = None) -> None:
    """GET /health — liveness always 200 once the process is up; readiness (DB
    reachable) reported alongside it. docker-compose healthchecks and
    `depends_on: condition: service_healthy` key off this — see conventions in
    docs/architecture.md.
    """
    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def health() -> JSONResponse:
        ready = True
        if readiness_check is not None:
            ready = await readiness_check()
        body = {"status": "ok" if ready else "degraded", "ready": ready}
        return JSONResponse(
            content=body,
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    app.include_router(router)
