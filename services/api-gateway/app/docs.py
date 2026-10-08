import httpx
from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from sfo_common.errors import BadGatewayError, NotFoundError

from app import proxy
from app.config import settings

router = APIRouter()


def _services() -> dict[str, str]:
    """Unique backend services (not one per resource) — see
    docs/architecture.md#api-documentation."""
    return settings.service_urls


@router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def docs_index() -> str:
    links = "\n".join(
        f'<li><a href="/docs/{name}">{name}</a></li>' for name in _services()
    )
    return f"""
    <html><head><title>SmartFoodOps API Docs</title></head>
    <body>
      <h1>SmartFoodOps — API Documentation</h1>
      <p>One entry per backend service, proxied through api-gateway. See
      docs/architecture.md for the system overview.</p>
      <ul>{links}</ul>
    </body></html>
    """


@router.get("/docs/{service}", response_class=HTMLResponse, include_in_schema=False)
async def service_docs(service: str) -> HTMLResponse:
    if service not in _services():
        raise NotFoundError(f"Unknown service '{service}'")
    return get_swagger_ui_html(openapi_url=f"/docs/{service}/openapi.json", title=f"{service} docs")


@router.get("/docs/{service}/openapi.json", include_in_schema=False)
async def service_openapi(service: str) -> JSONResponse:
    base_url = _services().get(service)
    if base_url is None:
        raise NotFoundError(f"Unknown service '{service}'")
    try:
        upstream = await proxy.clients[service].get(f"{base_url}/openapi.json")
        upstream.raise_for_status()
        spec = upstream.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BadGatewayError(f"Could not load the OpenAPI spec for '{service}'") from exc
    return JSONResponse(content=spec)
