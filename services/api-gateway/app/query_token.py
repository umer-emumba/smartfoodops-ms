"""Browsers' EventSource can't set an Authorization header, so live-tracking
streams (/v1/tracking/...) may pass the access token as `?access_token=`.
This pure-ASGI middleware moves it into the Authorization header before
anything else sees the request (the rate limiter keys on the token's user),
and drops it from the query string so it's never forwarded upstream. A real
Authorization header wins if both are sent.

Tokens in URLs can end up in logs: the gateway's access log records the path
only, never the query string."""

from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

_PREFIX = "/v1/tracking/"
_PARAM = "access_token"


class QueryTokenMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(_PREFIX):
            params = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            tokens = [value for key, value in params if key == _PARAM]
            if tokens:
                headers = list(scope["headers"])
                if not any(name == b"authorization" for name, _ in headers):
                    headers.append((b"authorization", f"Bearer {tokens[-1]}".encode("latin-1")))
                rest = [(key, value) for key, value in params if key != _PARAM]
                scope = {
                    **scope,
                    "headers": headers,
                    "query_string": urlencode(rest).encode("latin-1"),
                }
        await self.app(scope, receive, send)
