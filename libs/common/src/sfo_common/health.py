from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[bool]]


def add_health_routes(app: FastAPI, readiness_check: ReadinessCheck | None = None) -> None:
    """Liveness and readiness are separate on purpose:

    - GET /livez — 200 whenever the process can serve HTTP, with no dependency
      checks. An orchestrator restarts the container when this fails, so it
      must never fail just because the DB is down: that would restart every
      replica at once during a DB outage, which fixes nothing.
    - GET /readyz — 200 only when dependencies (DB, ...) are reachable. A
      failing replica is taken out of load balancing, not restarted.
    - GET /health — same as /readyz; kept for docker-compose healthchecks and
      `depends_on: condition: service_healthy`, which mean "ready" (see
      docs/architecture.md#conventions).
    """
    router = APIRouter(tags=["health"])

    @router.get("/livez")
    async def livez() -> dict:
        return {"status": "ok"}

    async def readiness() -> JSONResponse:
        ready = True
        if readiness_check is not None:
            ready = await readiness_check()
        body = {"status": "ok" if ready else "degraded", "ready": ready}
        return JSONResponse(
            content=body,
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    router.add_api_route("/readyz", readiness, methods=["GET"])
    router.add_api_route("/health", readiness, methods=["GET"])

    app.include_router(router)
