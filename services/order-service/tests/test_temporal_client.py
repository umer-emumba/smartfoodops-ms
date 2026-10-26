"""How signalling failures surface to API clients."""

import pytest
from app import temporal_client
from sfo_common.errors import ConflictError, ServiceUnavailableError
from sfo_contracts import OrderSagaSignal
from temporalio.service import RPCError, RPCStatusCode


class _Handle:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.sent: list[tuple[str, object]] = []

    async def signal(self, signal: str, payload: object) -> None:
        if self.error:
            raise self.error
        self.sent.append((signal, payload))


class _Client:
    def __init__(self, handle: _Handle) -> None:
        self.handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        self.workflow_ids.append(workflow_id)
        return self.handle


def _install(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> _Client:
    client = _Client(_Handle(error))

    async def _get_client() -> _Client:
        return client

    monkeypatch.setattr(temporal_client, "get_client", _get_client)
    return client


async def test_signals_the_orders_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    await temporal_client.signal_order_saga("o-1", OrderSagaSignal.CANCEL, {"x": 1})
    assert client.workflow_ids == ["order-o-1"]
    assert client.handle.sent == [(OrderSagaSignal.CANCEL, {"x": 1})]


async def test_no_running_workflow_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
    with pytest.raises(ConflictError):
        await temporal_client.signal_order_saga("o-1", OrderSagaSignal.CANCEL, None)


async def test_temporal_down_is_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b""))
    with pytest.raises(ServiceUnavailableError):
        await temporal_client.signal_order_saga("o-1", OrderSagaSignal.CANCEL, None)
