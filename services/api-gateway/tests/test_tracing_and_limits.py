"""Trace-ID propagation, per-request access logging, and the body-size limit."""

import logging

import httpx
from app.config import settings

from tests.helpers import auth as _auth
from tests.helpers import gateway_client as _client


async def test_trace_id_forwarded_to_backend_and_echoed(gateway) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.headers["x-request-id"]
        return httpx.Response(200, json=[])

    gateway(handler)
    async with _client() as c:
        r = await c.get("/v1/orders", headers={**_auth(), "X-Request-ID": "trace-abc-123"})
    assert r.headers["x-request-id"] == "trace-abc-123"
    assert seen["id"] == "trace-abc-123"


async def test_trace_id_generated_when_absent(gateway) -> None:
    gateway(lambda request: httpx.Response(200, json=[]))
    async with _client() as c:
        r = await c.get("/v1/orders", headers=_auth())
    assert len(r.headers["x-request-id"]) == 36  # uuid4


async def test_one_access_log_line_per_request_with_trace_id(gateway, caplog) -> None:
    gateway(lambda request: httpx.Response(201, json={}))
    with caplog.at_level(logging.INFO, logger="sfo.request"):
        async with _client() as c:
            await c.post("/v1/orders", headers={**_auth(), "X-Request-ID": "t-1"}, json={})
            await c.get("/livez")  # probes are not logged
    lines = [r for r in caplog.records if r.name == "sfo.request"]
    assert len(lines) == 1
    fields = lines[0].extra_fields
    assert fields["method"] == "POST"
    assert fields["path"] == "/v1/orders"
    assert fields["status"] == 201
    assert fields["duration_ms"] >= 0


async def test_oversized_body_rejected_before_reaching_backend(gateway, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(settings, "max_request_body_bytes", 100)
    gateway(lambda request: calls.append(1) or httpx.Response(200, json={}))
    async with _client() as c:
        r = await c.post("/v1/orders", headers=_auth(), content=b"x" * 101)
        ok = await c.post("/v1/orders", headers=_auth(), content=b"x" * 100)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert ok.status_code == 200
    assert calls == [1]  # only the in-limit request was forwarded


async def test_chunked_body_without_content_length_is_still_capped(gateway, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 100)
    gateway(lambda request: httpx.Response(200, json={}))

    async def chunks():
        for _ in range(5):
            yield b"x" * 30  # 150 bytes total, no Content-Length

    async with _client() as c:
        r = await c.post("/v1/orders", headers=_auth(), content=chunks())
    assert r.status_code == 413
