import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Request-ID"


def get_correlation_id() -> str:
    """Read the current request's correlation ID (set by CorrelationIdMiddleware).

    Used by logging/tracing setup and by outbound httpx/Kafka calls to propagate
    the same ID downstream — see docs/architecture.md#conventions.
    """
    return _correlation_id.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Reads X-Request-ID if the caller sent one, generates one otherwise, makes
    it available via get_correlation_id() for the duration of the request, and
    echoes it back on the response.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER, str(uuid.uuid4()))
        token = _correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
