import httpx
from fastapi import APIRouter, Depends, Request, Response
from sfo_common.errors import NotFoundError
from sfo_common.middleware import CORRELATION_ID_HEADER, get_correlation_id

from app.config import settings
from app.rate_limit import RateLimiter
from app.security import is_public_route, require_valid_bearer_token

router = APIRouter()

_client = httpx.AsyncClient(timeout=10.0)

# Hop-by-hop headers must not be forwarded as-is between the client and the
# backend — see RFC 7230 6.1. Host/content-length are recomputed by httpx.
_EXCLUDED_REQUEST_HEADERS = {"host", "content-length", "connection"}
_EXCLUDED_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}


async def forward_request(resource: str, rest_of_path: str, request: Request) -> Response:
    """Routes by the first /v1/{resource} path segment to the service that
    owns it — see docs/architecture.md#conventions. Not a response-aggregating
    BFF: one client call always maps to exactly one backend service call
    (docs/architecture.md#conventions, gateway scope). Shared by the generic
    catch-all route and the auth routes that need a stricter rate limit."""
    base_url = settings.service_registry.get(resource)
    if base_url is None:
        raise NotFoundError(f"No service owns resource '{resource}'")

    if not is_public_route(request.method, resource, rest_of_path):
        require_valid_bearer_token(request)

    target_url = f"{base_url}/v1/{resource}/{rest_of_path}".rstrip("/")

    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _EXCLUDED_REQUEST_HEADERS
    }
    forward_headers[CORRELATION_ID_HEADER] = get_correlation_id()

    body = await request.body()
    upstream_response = await _client.request(
        request.method,
        target_url,
        params=request.query_params,
        headers=forward_headers,
        content=body,
    )

    response_headers = {
        k: v for k, v in upstream_response.headers.items() if k.lower() not in _EXCLUDED_RESPONSE_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
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
