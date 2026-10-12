"""Stale-rider detector (week2-plan D12) against a real Postgres and Redis."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app import activities, stale_riders
from app.models import Delivery, Rider
from sfo_common import temporal
from sfo_contracts import OrderSagaSignal
from sfo_contracts.activities import TransitionDeliveryInput
from sfo_contracts.workflows import RiderDropped

from tests.conftest import Env
from tests.helpers import add_rider, assigned, km_north

pytestmark = pytest.mark.db


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, tuple[Any, ...]]]:
    signals: list[tuple[str, str, tuple[Any, ...]]] = []

    async def _signal(_client: object, order_id: str, signal: str, *args: Any) -> None:
        signals.append((order_id, signal, args))

    monkeypatch.setattr(temporal, "signal_order_saga", _signal)
    return signals


async def _age(env: Env, delivery_id: str, rider_id: str, *, minutes: float) -> None:
    """Rider silent for `minutes`, assigned at least that long ago."""
    then = datetime.now(UTC) - timedelta(minutes=minutes)
    async with env.factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        rider = await session.get(Rider, rider_id)
        assert delivery is not None and rider is not None
        delivery.assigned_at = then
        rider.last_location_at = then
        await session.commit()


async def test_silent_assigned_rider_is_signalled_once(env: Env, sent: list) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env, "o-1")
    await _age(env, delivery_id, "r1", minutes=4)

    assert await stale_riders.check_once(object()) == 1  # type: ignore[arg-type]
    assert await stale_riders.check_once(object()) == 0  # type: ignore[arg-type]
    assert sent == [
        (
            "o-1",
            OrderSagaSignal.RIDER_DROPPED,
            (RiderDropped(delivery_id=delivery_id, reason="no_location_updates"),),
        )
    ]


async def test_rider_still_pinging_is_left_alone(env: Env, sent: list) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await _age(env, delivery_id, "r1", minutes=2)  # under the 3-minute threshold
    assert await stale_riders.check_once(object()) == 0  # type: ignore[arg-type]
    assert sent == []


async def test_rider_with_the_food_is_never_reassigned(env: Env, sent: list) -> None:
    await add_rider(env, "r1", at=km_north(0.5))
    delivery_id, _ = await assigned(env)
    await activities.transition_delivery(
        TransitionDeliveryInput(delivery_id=delivery_id, to_status="picked_up")
    )
    await _age(env, delivery_id, "r1", minutes=10)
    assert await stale_riders.check_once(object()) == 0  # type: ignore[arg-type]
    assert sent == []
