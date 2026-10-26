"""RED metrics middleware (sfo_observability.metrics)."""

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sfo_observability import instrument_metrics


def _app() -> FastAPI:
    app = FastAPI()
    instrument_metrics(app)

    @app.get("/v1/orders/{order_id}")
    async def get_order(order_id: str) -> dict[str, str]:
        if order_id == "missing":
            raise HTTPException(404)
        return {"id": order_id}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _count(route: str, status: str) -> float:
    labels = {"method": "GET", "route": route, "status": status}
    return REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


async def test_requests_are_labelled_by_route_template_not_path() -> None:
    before_ok = _count("/v1/orders/{order_id}", "200")
    before_404 = _count("/v1/orders/{order_id}", "404")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/v1/orders/o-1")
        await c.get("/v1/orders/o-2")
        await c.get("/v1/orders/missing")
    assert _count("/v1/orders/{order_id}", "200") == before_ok + 2
    assert _count("/v1/orders/{order_id}", "404") == before_404 + 1
    assert _count("/v1/orders/o-1", "200") == 0  # no series per ID


async def test_unmatched_and_quiet_paths() -> None:
    before_unmatched = _count("unmatched", "404")
    before_health = _count("/health", "200")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/nope")
        await c.get("/health")
    assert _count("unmatched", "404") == before_unmatched + 1
    assert _count("/health", "200") == before_health  # probes aren't counted
