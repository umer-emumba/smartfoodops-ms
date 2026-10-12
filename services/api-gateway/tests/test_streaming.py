"""The /v1/tracking streaming proxy and the ?access_token= handling
(week2-plan step 7)."""

import asyncio

import httpx
import pytest
from app import proxy
from app.config import settings
from app.main import app
from httpx import ASGITransport, AsyncClient
from sfo_common.testing import serve
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from tests.helpers import auth


def _token() -> str:
    return auth()["Authorization"][7:]


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw")


async def test_query_token_becomes_the_authorization_header(gateway, client) -> None:
    seen: dict[str, object] = {}

    def backend(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"event: snapshot\ndata: {}\n\n"),
        )

    gateway(backend)
    resp = await client.get(f"/v1/tracking/orders/o-1?access_token={_token()}&since=5")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == "event: snapshot\ndata: {}\n\n"
    assert seen["auth"] == f"Bearer {_token()}"
    # The token is never forwarded in the URL; other parameters are.
    assert seen["query"] == {"since": "5"}


async def test_tracking_requires_a_token(gateway, client) -> None:
    gateway(lambda _: httpx.Response(200))
    assert (await client.get("/v1/tracking/orders/o-1")).status_code == 401
    bad = await client.get("/v1/tracking/orders/o-1?access_token=not-a-jwt")
    assert bad.status_code == 401


async def test_upstream_errors_pass_through_as_json(gateway, client) -> None:
    gateway(lambda _: httpx.Response(403, json={"error": {"code": "forbidden"}}))
    resp = await client.get("/v1/tracking/orders/o-1", headers=auth())
    assert (resp.status_code, resp.json()) == (403, {"error": {"code": "forbidden"}})


async def test_path_traversal_is_refused(gateway, client) -> None:
    gateway(lambda _: httpx.Response(200))
    # Encoded so the client doesn't normalise the dots away before sending.
    resp = await client.get("/v1/tracking/orders/%2E%2E/%2E%2E/openapi.json", headers=auth())
    assert resp.status_code == 404


async def test_events_arrive_as_they_are_sent_not_at_the_end(
    gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real servers on both sides: the upstream sends one event, then waits
    until the test has *received* it before sending the next. If the gateway
    buffered, the first event would never arrive and this would time out."""
    first_received = asyncio.Event()

    async def events():  # type: ignore[no-untyped-def]
        yield b'event: snapshot\ndata: {"n":1}\n\n'
        await first_received.wait()
        yield b'event: status\ndata: {"n":2}\n\n'

    async def endpoint(_request):  # type: ignore[no-untyped-def]
        return StreamingResponse(events(), media_type="text/event-stream")

    upstream_app = Starlette(routes=[Route("/v1/tracking/orders/o-1", endpoint)])
    gateway(lambda _: httpx.Response(500))  # stubs Redis; real client set below
    async with serve(upstream_app) as upstream_url, serve(app) as gateway_url:
        monkeypatch.setattr(settings, "tracking_service_url", upstream_url)
        monkeypatch.setitem(proxy.clients, "tracking-service", httpx.AsyncClient())
        async with httpx.AsyncClient(base_url=gateway_url, timeout=5) as http:
            async with http.stream("GET", "/v1/tracking/orders/o-1", headers=auth()) as resp:
                lines = resp.aiter_lines()
                first = await asyncio.wait_for(anext(lines), timeout=3)
                assert first == "event: snapshot"
                first_received.set()
                rest = [line async for line in lines]
    assert "event: status" in rest
