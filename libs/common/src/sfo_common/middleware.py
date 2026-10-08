import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Request-ID"

# A client-supplied ID is echoed into every log line and downstream call, so
# it's only accepted if it looks like an ID — otherwise a caller could inject
# newlines or megabytes of junk into the logs.
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Orchestrator/compose probes hit these every few seconds; logging them would
# drown real traffic.
_UNLOGGED_PATHS = frozenset({"/health", "/livez", "/readyz"})

_request_logger = logging.getLogger("sfo.request")


def get_correlation_id() -> str:
    """Read the current request's correlation ID (set by CorrelationIdMiddleware).

    Used by logging (emitted as `trace_id`) and by outbound httpx/Kafka calls
    to propagate the same ID downstream — see docs/architecture.md#conventions.
    """
    return _correlation_id.get()


async def propagate_correlation_id(request: Any) -> None:
    """httpx `request` event hook for service-to-service clients: forwards
    the current request's ID, so the called service logs under the same
    trace instead of starting a new one. Usage:
    `httpx.AsyncClient(event_hooks={"request": [propagate_correlation_id]})`.
    """
    correlation_id = get_correlation_id()
    if correlation_id:
        request.headers[CORRELATION_ID_HEADER] = correlation_id


class CorrelationIdMiddleware:
    """Per request: reads X-Request-ID if the caller sent a valid one (else
    generates one), exposes it via get_correlation_id() for every log line and
    downstream call, echoes it on the response, and writes one access-log line
    (method, path, status, duration) under it.

    Pure ASGI rather than BaseHTTPMiddleware: no per-request task/stream
    wrapping, and it doesn't buffer or break streaming responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(CORRELATION_ID_HEADER, "")
        correlation_id = incoming if _VALID_CORRELATION_ID.match(incoming) else str(uuid.uuid4())
        # Deliberately never reset: the shared "Unhandled error" handler runs
        # outside this middleware (in Starlette's ServerErrorMiddleware), and
        # needs the ID still set to log the crash under the right trace. Each
        # request runs in its own task, so the value can't leak to another.
        _correlation_id.set(correlation_id)
        started = time.perf_counter()
        status_code = 500  # if the app raises before sending a response

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            if scope["path"] not in _UNLOGGED_PATHS:
                _request_logger.info(
                    "%s %s %s",
                    scope["method"],
                    scope["path"],
                    status_code,
                    extra={
                        "extra_fields": {
                            "method": scope["method"],
                            "path": scope["path"],
                            "status": status_code,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        }
                    },
                )
