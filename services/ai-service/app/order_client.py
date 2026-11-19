"""Reads an order's facts for an explanation (docs/week5-plan.md step 4)
from order-, delivery- and payment-service's public APIs — **as the
customer**, with their own bearer token. So the ownership rules are exactly
those of `GET /v1/orders/{id}`: ai-service sees an order only if the
customer could.

The order is required: not found / not yours → the same 404 / 403, the
service down → 503. Delivery and payment are optional (an order may have
neither yet, and a failed read only makes the explanation less precise)."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sfo_common.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from sfo_common.middleware import propagate_correlation_id

from app.config import settings

logger = logging.getLogger(__name__)


def _client(url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url,
        timeout=httpx.Timeout(settings.order_facts_timeout_seconds),
        event_hooks={"request": [propagate_correlation_id]},
    )


orders = _client(settings.order_service_url)
deliveries = _client(settings.delivery_service_url)
payments = _client(settings.payment_service_url)


@dataclass(frozen=True)
class OrderRecord:
    """What the explanation is built from: the services' JSON, as is."""

    order: dict[str, Any]
    delivery: dict[str, Any] | None
    payment: dict[str, Any] | None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _optional(client: httpx.AsyncClient, path: str, token: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(path, headers=_auth(token))
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        return dict(resp.json())
    except (httpx.HTTPError, ValueError):
        logger.warning("Couldn't read %s; explaining without it", path, exc_info=True)
        return None


async def order(token: str, order_id: str) -> dict[str, Any]:
    try:
        resp = await orders.get(f"/v1/orders/{order_id}", headers=_auth(token))
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError("Orders are unavailable right now") from exc
    if resp.status_code == 404:
        raise NotFoundError("Order not found")
    if resp.status_code == 403:
        raise ForbiddenError("Not your order")
    if resp.status_code >= 400:
        raise ServiceUnavailableError("Orders are unavailable right now")
    return dict(resp.json())


async def record(token: str, order_id: str) -> OrderRecord:
    found = await order(token, order_id)
    delivery, payment = await asyncio.gather(
        _optional(deliveries, f"/v1/deliveries/by-order/{order_id}", token),
        _optional(payments, f"/v1/payments/by-order/{order_id}", token),
    )
    return OrderRecord(found, delivery, payment)


async def latest_order_id(token: str) -> str | None:
    """The customer's most recent order (the chat's "where is my order?")."""
    try:
        resp = await orders.get("/v1/orders", params={"limit": 1}, headers=_auth(token))
        resp.raise_for_status()
        items = resp.json().get("items") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        logger.warning("Couldn't list the customer's orders", exc_info=True)
        return None
    return str(items[0]["id"]) if items else None
