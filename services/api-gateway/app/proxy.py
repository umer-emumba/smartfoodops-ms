from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, Request, Response
from sfo_common.errors import (
    BadGatewayError,
    GatewayTimeoutError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
)
from sfo_common.middleware import CORRELATION_ID_HEADER, get_correlation_id

from app.config import settings
from app.rate_limit import RateLimiter
from app.security import authenticate, is_public_route

router = APIRouter()


@dataclass(frozen=True)
class _UpstreamPolicy:
    max_connections: int
    timeout_seconds: float


# One connection pool *per backend service*, not one shared pool (a
# bulkhead): a backend that slows down can only fill its own pool, so a slow
# order-service can't starve requests to a healthy restaurant-service. Limits
# are per gateway replica; size them roughly as peak req/s to that service x
# normal response time x ~10 headroom, and tune from load tests.
# order-service's longer timeout covers order creation's own downstream call.
_UPSTREAM_POLICIES: dict[str, _UpstreamPolicy] = {
    "user-service": _UpstreamPolicy(max_connections=100, timeout_seconds=5.0),
    "restaurant-service": _UpstreamPolicy(max_connections=150, timeout_seconds=5.0),
    "order-service": _UpstreamPolicy(max_connections=100, timeout_seconds=10.0),
    "delivery-service": _UpstreamPolicy(max_connections=100, timeout_seconds=5.0),
    "payment-service": _UpstreamPolicy(max_connections=50, timeout_seconds=5.0),
    "notification-service": _UpstreamPolicy(max_connections=50, timeout_seconds=5.0),
}


def _make_client(policy: _UpstreamPolicy) -> httpx.AsyncClient:
    # connect: a dead backend fails fast (502). read: bounds a backend that
    # accepted but hangs (504). pool: a request that can't get a connection
    # because the pool is full fails fast (503) instead of queueing behind
    # the slow requests that filled it.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            policy.timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
            pool=settings.upstream_pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=policy.max_connections,
            max_keepalive_connections=policy.max_connections // 2,
        ),
    )


# Keyed by service name (settings.service_urls). Closed on shutdown by
# app.main's lifespan.
clients: dict[str, httpx.AsyncClient] = {
    name: _make_client(_UPSTREAM_POLICIES[name]) for name in settings.service_urls
}

# Hop-by-hop headers (RFC 9110 §7.6.1) describe one connection, not the
# request, so they must not be forwarded — plus any header the Connection
# header itself names. Host and Content-Length are recomputed by httpx for
# the new request; X-Request-ID is replaced with the gateway's validated one.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_EXCLUDED_REQUEST_HEADERS = _HOP_BY_HOP | {"host", "content-length", CORRELATION_ID_HEADER.lower()}
# httpx has already decoded the body (e.g. un-gzipped it), so the upstream's
# Content-Encoding/Length no longer describe what's being sent back.
_EXCLUDED_RESPONSE_HEADERS = _HOP_BY_HOP | {"content-length", "content-encoding"}


def _filter_headers(headers: Mapping[str, str], excluded: set[str]) -> dict[str, str]:
    connection_listed = {
        h.strip().lower() for h in headers.get("connection", "").split(",") if h.strip()
    }
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in excluded and k.lower() not in connection_listed
    }


async def _read_body_capped(request: Request) -> bytes:
    """The request body, refusing anything over max_request_body_bytes.

    A too-large declared Content-Length is rejected before reading a byte; a
    body without one (chunked) is counted as it streams, so neither can make
    the gateway buffer more than the limit."""
    limit = settings.max_request_body_bytes
    too_large = PayloadTooLargeError("Request body is too large", details={"max_bytes": limit})
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


async def forward_request(resource: str, rest_of_path: str, request: Request) -> Response:
    """Routes by the first /v1/{resource} path segment to the service that
    owns it — see docs/architecture.md#conventions. Not a response-aggregating
    BFF: one client call always maps to exactly one backend service call
    (docs/architecture.md#conventions, gateway scope). Shared by the generic
    catch-all route and the auth routes that need a stricter rate limit."""
    service = settings.service_registry.get(resource)
    if service is None:
        raise NotFoundError(f"No service owns resource '{resource}'")
    base_url = settings.service_urls[service]
    # httpx would collapse ".." segments, letting an authenticated caller
    # reach a backend path outside /v1/{resource}/ (e.g. /openapi.json).
    if ".." in rest_of_path.split("/"):
        raise NotFoundError("Not found")

    await authenticate(request, public=is_public_route(request.method, resource, rest_of_path))

    target_url = f"{base_url}/v1/{resource}/{rest_of_path}".rstrip("/")
    forward_headers = _filter_headers(request.headers, _EXCLUDED_REQUEST_HEADERS)
    forward_headers[CORRELATION_ID_HEADER] = get_correlation_id()

    try:
        upstream_response = await clients[service].request(
            request.method,
            target_url,
            params=request.query_params,
            headers=forward_headers,
            content=await _read_body_capped(request),
        )
    except httpx.PoolTimeout as exc:
        # Checked before TimeoutException (its parent): the backend wasn't
        # slow on *this* request, its pool is full of other in-flight ones.
        raise ServiceUnavailableError(f"'{resource}' service is busy, please retry") from exc
    except httpx.TimeoutException as exc:
        raise GatewayTimeoutError(f"'{resource}' service timed out") from exc
    except httpx.RequestError as exc:
        raise BadGatewayError(f"'{resource}' service is unavailable") from exc

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_filter_headers(upstream_response.headers, _EXCLUDED_RESPONSE_HEADERS),
    )


_default_rate_limit = Depends(
    RateLimiter(times=settings.rate_limit_default_per_minute, seconds=60, bucket="default")
)


# Two routes, one handler: "/v1/{resource}/{rest_of_path:path}" requires at
# least one "/" after {resource} to match at all (Starlette's `:path`
# converter needs the separator present, even to capture an empty string) —
# without this bare-resource route, a call like `POST /v1/restaurants` (no
# trailing segment) 307-redirects to "/v1/restaurants/" instead of reaching
# this handler, and most HTTP clients (including httpx.Client's default
# config) don't auto-follow redirects, especially for non-GET methods.
@router.api_route(
    "/v1/{resource}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    dependencies=[_default_rate_limit],
)
@router.api_route(
    "/v1/{resource}/{rest_of_path:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    dependencies=[_default_rate_limit],
)
async def proxy(resource: str, *, rest_of_path: str = "", request: Request) -> Response:
    return await forward_request(resource, rest_of_path, request)
