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


# --- ai-service's streamed answers (docs/week5-plan.md step 0) ---


async def test_ai_chat_streams_and_forwards_the_body(gateway, client) -> None:
    seen: dict[str, object] = {}

    def backend(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"event: status\ndata: {}\n\nevent: done\ndata: {}\n\n"),
        )

    gateway(backend)
    resp = await client.post("/v1/ai/chat", json={"message": "something spicy"}, headers=auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == "event: status\ndata: {}\n\nevent: done\ndata: {}\n\n"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{settings.ai_service_url}/v1/ai/chat"
    assert seen["body"] == b'{"message":"something spicy"}'


async def test_ai_streams_use_their_own_pool(gateway, client) -> None:
    gateway(lambda _: httpx.Response(200))
    used: list[str] = []

    class Spy(httpx.AsyncClient):
        async def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
            used.append(str(request.url))
            return httpx.Response(200, json={}, request=request)

    proxy.clients["ai-service-stream"] = Spy()
    await client.get("/v1/ai/orders/o-1/explain", headers=auth())
    assert used == [f"{settings.ai_service_url}/v1/ai/orders/o-1/explain"]


async def test_ai_streams_require_a_token(gateway, client) -> None:
    gateway(lambda _: httpx.Response(200))
    assert (await client.post("/v1/ai/chat", json={})).status_code == 401
    assert (await client.get("/v1/ai/orders/o-1/explain")).status_code == 401


async def test_ai_streams_have_their_own_rate_limit_bucket(
    gateway, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import rate_limit

    gateway(lambda _: httpx.Response(200, json={}))
    keys: list[str] = []

    async def over_limit(key, _window, deny_key):  # type: ignore[no-untyped-def]
        keys.append(key)
        return settings.rate_limit_ai_per_minute + 1, 30_000, (False if deny_key else None)

    monkeypatch.setattr(rate_limit, "_redis_checks", over_limit)
    resp = await client.post("/v1/ai/chat", json={}, headers=auth())
    assert resp.status_code == 429
    assert keys == ["rate_limit:ai:user:u1"]


async def test_ai_search_is_not_streamed_or_ai_limited(gateway, client) -> None:
    """Plain ai-service calls still go through the buffered catch-all."""
    seen: list[str] = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"hits": []})

    gateway(backend)
    resp = await client.get("/v1/ai/search?q=biryani")
    assert resp.status_code == 200
    assert seen == [f"{settings.ai_service_url}/v1/ai/search?q=biryani"]
