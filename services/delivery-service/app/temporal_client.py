"""Rider endpoints signal the order saga through this lazily connected
client (docs/week2-plan.md §2): accept/decline, drop-out, picked up,
delivered."""

from typing import Any

from sfo_common import temporal
from sfo_common.temporal import lazy_client
from temporalio.client import Client

from app.config import settings

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await lazy_client(settings)
    return _client


async def signal_order_saga(order_id: str, signal: str, *args: Any) -> None:
    await temporal.signal_order_saga(await get_client(), order_id, signal, *args)
