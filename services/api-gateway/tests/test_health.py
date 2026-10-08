import pytest
from app.main import app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sfo_common.health import add_health_routes


@pytest.mark.asyncio
async def test_health_reports_status() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code in (200, 503)
    assert "status" in response.json()


@pytest.mark.asyncio
async def test_liveness_ignores_dependencies_readiness_does_not() -> None:
    async def dependency_down() -> bool:
        return False

    probe_app = FastAPI()
    add_health_routes(probe_app, readiness_check=dependency_down)
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://t") as c:
        # A dependency outage must not fail liveness (that would restart
        # every replica), only readiness.
        assert (await c.get("/livez")).status_code == 200
        assert (await c.get("/readyz")).status_code == 503
        assert (await c.get("/health")).status_code == 503
