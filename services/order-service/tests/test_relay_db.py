"""The outbox relay (sfo_events.relay, docs/week3-plan.md W3-D2) against the
real order database; Kafka and Schema Registry are faked."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models import OutboxEvent
from sfo_events import DomainEvent, EventType, new_outbox_row
from sfo_events.relay import RELAY_LOCK_KEY, OutboxRelay
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeSerializer:
    def __init__(self, poison: set[str] | None = None) -> None:
        self.poison = poison or set()

    def serialize(self, event: DomainEvent) -> bytes:
        if event.event_id in self.poison:
            raise ValueError("schema says no")
        return json.dumps(event.model_dump(mode="json")).encode()


class FakeProducer:
    """Delivers on flush(); messages whose key is in `down_keys` fail like
    an unreachable broker."""

    def __init__(self, down_keys: set[str] | None = None) -> None:
        self.down_keys = down_keys or set()
        self.sent: list[dict[str, Any]] = []
        self._pending: list[tuple[str, Callable[..., None]]] = []

    def produce(
        self, topic: str, *, key: bytes, value: bytes, headers: Any, on_delivery: Any
    ) -> None:
        self._pending.append((key.decode(), on_delivery))
        if key.decode() not in self.down_keys:
            self.sent.append({"topic": topic, "key": key.decode(), "event": json.loads(value)})

    def flush(self, timeout: float) -> int:
        for key, callback in self._pending:
            callback("Local: Message timed out" if key in self.down_keys else None, None)
        self._pending = []
        return 0


def _relay(factory: Factory, producer: FakeProducer, serializer: FakeSerializer) -> OutboxRelay:
    return OutboxRelay(
        session_factory=factory,
        outbox_model=OutboxEvent,
        bootstrap_servers="unused",
        schema_registry_url="unused",
        producer=producer,  # type: ignore[arg-type]
        serializer=serializer,  # type: ignore[arg-type]
    )


async def _add(factory: Factory, order_id: str, event_type: EventType, minute: int) -> str:
    row = new_outbox_row(
        OutboxEvent,
        aggregate_id=order_id,
        event_type=event_type,
        payload={
            "order_id": order_id,
            "order_number": 1000,
            "customer_id": "u-customer",
            "restaurant_id": "r-1",
            "restaurant_admin_id": "u-owner",
            "total_amount": "100.00",
            "from_status": "created",
            "to_status": "x",
        },
    )
    row.created_at = T0 + timedelta(minutes=minute)
    async with factory() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _rows(factory: Factory) -> dict[str, OutboxEvent]:
    async with factory() as session:
        return {r.id: r for r in (await session.execute(select(OutboxEvent))).scalars()}


async def test_publishes_oldest_first_keyed_by_aggregate(session_factory: Factory) -> None:
    second = await _add(session_factory, "o-1", EventType.ORDER_CONFIRMED, minute=2)
    first = await _add(session_factory, "o-1", EventType.ORDER_AWAITING_RESTAURANT, minute=1)
    other = await _add(session_factory, "o-2", EventType.ORDER_CANCELLED, minute=3)
    producer = FakeProducer()

    result = await _relay(session_factory, producer, FakeSerializer()).relay_once()

    assert (result.published, result.failed) == (3, 0)
    assert [m["event"]["event_id"] for m in producer.sent] == [first, second, other]
    assert [(m["topic"], m["key"]) for m in producer.sent] == [
        ("order-events", "o-1"),
        ("order-events", "o-1"),
        ("order-events", "o-2"),
    ]
    # occurred_at is the row's created_at.
    assert producer.sent[0]["event"]["occurred_at"] == "2026-10-15T12:01:00Z"
    assert all(r.dispatched_at is not None for r in (await _rows(session_factory)).values())
    # Nothing left: the next tick publishes nothing.
    assert (await _relay(session_factory, producer, FakeSerializer()).relay_once()).published == 0


async def test_kafka_failure_is_recorded_and_retried(session_factory: Factory) -> None:
    row_id = await _add(session_factory, "o-1", EventType.ORDER_CONFIRMED, minute=1)

    down = await _relay(session_factory, FakeProducer({"o-1"}), FakeSerializer()).relay_once()
    assert (down.published, down.failed) == (0, 1)
    row = (await _rows(session_factory))[row_id]
    assert row.dispatched_at is None
    assert (row.publish_attempts, row.last_error) == (1, "kafka: Local: Message timed out")

    back = await _relay(session_factory, FakeProducer(), FakeSerializer()).relay_once()
    assert back.published == 1
    row = (await _rows(session_factory))[row_id]
    assert row.dispatched_at is not None and row.last_error is None


async def test_unserializable_row_holds_back_only_its_own_aggregate(
    session_factory: Factory,
) -> None:
    bad = await _add(session_factory, "o-1", EventType.ORDER_CONFIRMED, minute=1)
    later_same = await _add(session_factory, "o-1", EventType.RIDER_ASSIGNED, minute=2)
    other = await _add(session_factory, "o-2", EventType.ORDER_CONFIRMED, minute=3)
    producer = FakeProducer()

    result = await _relay(session_factory, producer, FakeSerializer({bad})).relay_once()

    assert [m["event"]["event_id"] for m in producer.sent] == [other]
    rows = await _rows(session_factory)
    assert rows[bad].last_error == "serialize: schema says no"
    # Not sent, so it can't overtake the failed event; untouched.
    assert (rows[later_same].dispatched_at, rows[later_same].publish_attempts) == (None, 0)
    assert (result.published, result.failed) == (1, 1)


async def test_only_one_relay_at_a_time(session_factory: Factory) -> None:
    await _add(session_factory, "o-1", EventType.ORDER_CONFIRMED, minute=1)
    producer = FakeProducer()
    async with session_factory() as other_relay:
        await other_relay.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": RELAY_LOCK_KEY}
        )
        result = await _relay(session_factory, producer, FakeSerializer()).relay_once()
    assert result.locked_out and producer.sent == []


async def test_purge_deletes_only_old_dispatched_rows(session_factory: Factory) -> None:
    old = await _add(session_factory, "o-1", EventType.ORDER_CONFIRMED, minute=1)
    recent = await _add(session_factory, "o-2", EventType.ORDER_CONFIRMED, minute=2)
    pending = await _add(session_factory, "o-3", EventType.ORDER_CONFIRMED, minute=3)
    now = datetime.now(UTC)
    async with session_factory() as session:
        for row_id, dispatched in ((old, now - timedelta(days=8)), (recent, now)):
            row = await session.get(OutboxEvent, row_id)
            assert row is not None
            row.dispatched_at = dispatched
        await session.commit()

    relay = _relay(session_factory, FakeProducer(), FakeSerializer())
    assert await relay.purge_dispatched(timedelta(days=7)) == 1
    assert set(await _rows(session_factory)) == {recent, pending}
