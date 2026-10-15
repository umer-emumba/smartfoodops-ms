"""The consumer loop and the idempotency wrapper, with fake Kafka and a fake
session — no broker or database needed."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from confluent_kafka import KafkaError, TopicPartition
from sfo_events import DomainEvent, EventType, idempotent
from sfo_events.consumer import ConsumerLoop, DeadLetterError, Outcome, is_transient
from sfo_events.kafka import header_value
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.samples import sample_event


class FakeMessage:
    def __init__(self, value: bytes, offset: int = 0, error: KafkaError | None = None) -> None:
        self._value, self._offset, self._error = value, offset, error

    def error(self) -> KafkaError | None:
        return self._error

    def topic(self) -> str:
        return "order-events"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset

    def key(self) -> bytes:
        return b"agg-1"

    def value(self) -> bytes:
        return self._value

    def headers(self) -> list[tuple[str, bytes]]:
        return [("event_type", b"OrderConfirmed")]


class FakeConsumer:
    """Serves `messages` in order; seek() rewinds like Kafka would."""

    def __init__(self, messages: list[FakeMessage] | None = None) -> None:
        self.committed: list[int] = []
        self.seeks: list[int] = []
        self._messages = messages or []
        self._position = 0

    def poll(self, timeout: float) -> FakeMessage | None:
        if self._position >= len(self._messages):
            return None
        msg = self._messages[self._position]
        self._position += 1
        return msg

    def commit(self, *, message: FakeMessage, asynchronous: bool) -> None:
        assert asynchronous is False
        self.committed.append(message.offset())

    def seek(self, partition: TopicPartition) -> None:
        self.seeks.append(partition.offset)
        self._position = next(
            i for i, m in enumerate(self._messages) if m.offset() == partition.offset
        )


class FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._fail = fail

    def produce(self, topic: str, **kwargs: Any) -> None:
        self.sent.append({"topic": topic, **kwargs})

    def flush(self, timeout: float) -> int:
        return 1 if self._fail else 0


def _message(event: DomainEvent, offset: int = 0) -> FakeMessage:
    return FakeMessage(json.dumps(event.model_dump(mode="json")).encode(), offset)


def _deserialize(msg: FakeMessage) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(msg.value())
    return data


async def _no_sleep(_: float) -> None:
    return None


def _loop(
    process: Callable[[DomainEvent], Any],
    consumer: FakeConsumer,
    producer: FakeProducer,
    sleeps: list[float] | None = None,
) -> ConsumerLoop:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return ConsumerLoop(
        consumer=consumer,
        deserialize=_deserialize,  # type: ignore[arg-type]
        process=process,
        dlq_producer=producer,
        group="test-group",
        sleep=sleep,
    )


# --- the loop ---------------------------------------------------------------


async def test_processed_event_commits_its_offset() -> None:
    seen: list[str] = []

    async def process(event: DomainEvent) -> None:
        seen.append(event.event_id)

    consumer, producer = FakeConsumer(), FakeProducer()
    outcome = await _loop(process, consumer, producer).handle(
        _message(sample_event(EventType.ORDER_CONFIRMED), offset=7)  # type: ignore[arg-type]
    )
    assert outcome is Outcome.PROCESSED
    assert seen == ["evt-1"]
    assert consumer.committed == [7]
    assert producer.sent == []


async def test_poison_message_goes_to_dlq_without_retries() -> None:
    calls: list[DomainEvent] = []

    async def process(event: DomainEvent) -> None:
        calls.append(event)

    consumer, producer = FakeConsumer(), FakeProducer()
    outcome = await _loop(process, consumer, producer).handle(
        FakeMessage(b"not json", offset=3)  # type: ignore[arg-type]
    )
    assert outcome is Outcome.DEAD_LETTERED
    assert calls == []
    assert consumer.committed == [3]
    [dead] = producer.sent
    assert dead["topic"] == "order-events.dlq"
    assert dead["value"] == b"not json" and dead["key"] == b"agg-1"
    assert header_value(dead["headers"], "dlq.original.offset") == "3"
    assert header_value(dead["headers"], "dlq.consumer.group") == "test-group"
    assert header_value(dead["headers"], "dlq.attempts") == "0"
    # The original headers are kept.
    assert header_value(dead["headers"], "event_type") == "OrderConfirmed"


async def test_failing_handler_is_retried_with_backoff_then_dead_lettered() -> None:
    attempts = 0

    async def process(event: DomainEvent) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("db down")

    consumer, producer, sleeps = FakeConsumer(), FakeProducer(), []
    outcome = await _loop(process, consumer, producer, sleeps).handle(
        _message(sample_event(EventType.ORDER_CONFIRMED))  # type: ignore[arg-type]
    )
    assert outcome is Outcome.DEAD_LETTERED
    assert attempts == 4  # first try + 3 retries
    assert sleeps == [0.5, 1.0, 2.0]
    [dead] = producer.sent
    assert header_value(dead["headers"], "dlq.error.type") == "RuntimeError"
    assert header_value(dead["headers"], "dlq.error.message") == "db down"
    assert header_value(dead["headers"], "dlq.attempts") == "4"
    assert consumer.committed == [0]


async def test_transient_failure_recovers_without_dlq() -> None:
    attempts = 0

    async def process(event: DomainEvent) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("blip")

    consumer, producer = FakeConsumer(), FakeProducer()
    outcome = await _loop(process, consumer, producer).handle(
        _message(sample_event(EventType.ORDER_CONFIRMED))  # type: ignore[arg-type]
    )
    assert outcome is Outcome.PROCESSED
    assert producer.sent == []


async def test_dlq_failure_leaves_the_offset_uncommitted() -> None:
    consumer = FakeConsumer()

    async def process(event: DomainEvent) -> None:
        return None

    with pytest.raises(DeadLetterError):
        await _loop(process, consumer, FakeProducer(fail=True)).handle(
            FakeMessage(b"not json")  # type: ignore[arg-type]
        )
    assert consumer.committed == []


async def test_unknown_event_type_is_skipped_and_committed() -> None:
    wire = sample_event(EventType.ORDER_CONFIRMED).model_dump(mode="json")
    wire["event_type"] = "SomethingNew"
    consumer, producer = FakeConsumer(), FakeProducer()

    async def process(event: DomainEvent) -> None:
        raise AssertionError("not called")

    outcome = await _loop(process, consumer, producer).handle(
        FakeMessage(json.dumps(wire).encode())  # type: ignore[arg-type]
    )
    assert outcome is Outcome.SKIPPED
    assert consumer.committed == [0] and producer.sent == []


async def test_kafka_errors_are_not_committed() -> None:
    consumer = FakeConsumer()

    async def process(event: DomainEvent) -> None:
        raise AssertionError("not called")

    msg = FakeMessage(b"", error=KafkaError(KafkaError._PARTITION_EOF))
    assert await _loop(process, consumer, FakeProducer()).handle(msg) is None  # type: ignore[arg-type]
    assert consumer.committed == []


# --- outages vs bad messages ----------------------------------------------


class RegistryDown(Exception):
    """Named like the registry client's (vendored httpx) error."""


class TransportError(RegistryDown):
    pass


class RegistryHttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.http_status_code = status


def _wrapped(cause: BaseException) -> Exception:
    try:
        raise cause
    except BaseException as inner:
        try:
            raise ValueError("wrapper") from inner
        except ValueError as outer:
            return outer


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ConnectionRefusedError(), True),
        (TimeoutError(), True),
        (OperationalError("SELECT 1", {}, Exception("connection refused")), True),
        (TransportError("refused"), True),
        (RegistryHttpError(503), True),
        (_wrapped(ConnectionResetError()), True),
        (ValueError("bad payload"), False),
        (IntegrityError("insert", {}, Exception("dup")), False),
        (RegistryHttpError(422), False),
        (RuntimeError("handler bug"), False),
    ],
)
def test_is_transient(exc: BaseException, transient: bool) -> None:
    assert is_transient(exc) is transient


async def test_outage_is_not_dead_lettered_or_committed() -> None:
    async def process(event: DomainEvent) -> None:
        raise ConnectionRefusedError("postgres down")

    consumer, producer = FakeConsumer(), FakeProducer()
    with pytest.raises(ConnectionRefusedError):
        await _loop(process, consumer, producer).handle(
            _message(sample_event(EventType.ORDER_CONFIRMED))  # type: ignore[arg-type]
        )
    assert consumer.committed == [] and producer.sent == []


async def test_registry_outage_while_deserializing_is_not_dead_lettered() -> None:
    def deserialize(msg: FakeMessage) -> dict[str, Any]:
        raise TransportError("registry down")

    consumer, producer = FakeConsumer(), FakeProducer()
    loop = ConsumerLoop(
        consumer=consumer,
        deserialize=deserialize,  # type: ignore[arg-type]
        process=lambda e: asyncio.sleep(0),
        dlq_producer=producer,
        group="g",
    )
    with pytest.raises(TransportError):
        await loop.handle(FakeMessage(b"{}"))  # type: ignore[arg-type]
    assert consumer.committed == [] and producer.sent == []


async def test_run_rewinds_and_retries_until_the_outage_ends() -> None:
    stop = asyncio.Event()
    failures = 3
    seen: list[str] = []

    async def process(event: DomainEvent) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise ConnectionRefusedError("postgres down")
        seen.append(event.event_id)
        if len(seen) == 2:
            stop.set()

    first, second = (
        sample_event(EventType.ORDER_CONFIRMED, "e1"),
        sample_event(EventType.ORDER_DELIVERED, "e2"),
    )
    consumer = FakeConsumer([_message(first, offset=0), _message(second, offset=1)])
    sleeps: list[float] = []
    loop = _loop(process, consumer, FakeProducer(), sleeps)
    await asyncio.wait_for(loop.run(stop), timeout=5)

    assert seen == ["e1", "e2"]  # nothing skipped, order kept
    assert consumer.seeks == [0, 0, 0]
    assert sleeps == [0.5, 1.0, 2.0]  # backoff, reset after success
    assert consumer.committed == [0, 1]


# --- idempotency ------------------------------------------------------------


class Processed:
    def __init__(self, event_id: str, consumer: str) -> None:
        self.event_id, self.consumer = event_id, consumer


class FakeDb:
    """Committed `processed_events` rows plus business effects, shared by
    every session — like one database."""

    def __init__(self) -> None:
        self.processed: dict[str, Processed] = {}
        self.effects: list[str] = []
        self.commit_conflict = False


class FakeSession:
    def __init__(self, db: FakeDb) -> None:
        self._db = db
        self._pending: list[Any] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, model: type, key: str) -> Processed | None:
        return self._db.processed.get(key)

    def add(self, row: Any) -> None:
        self._pending.append(row)

    async def commit(self) -> None:
        if self._db.commit_conflict:
            # A concurrent delivery of the same event commits first.
            self._db.processed["evt-1"] = Processed("evt-1", "other")
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        for row in self._pending:
            if isinstance(row, Processed):
                self._db.processed[row.event_id] = row
            else:
                self._db.effects.append(row)
        self._pending = []

    async def rollback(self) -> None:
        self._pending = []


def _idempotent(db: FakeDb) -> Callable[[DomainEvent], Any]:
    async def handler(session: Any, event: DomainEvent) -> None:
        session.add(f"notify {event.event_id}")

    return idempotent(lambda: FakeSession(db), Processed, "notification-service", handler)


async def test_duplicate_event_is_a_no_op() -> None:
    db = FakeDb()
    consumer = FakeConsumer()
    loop = _loop(_idempotent(db), consumer, FakeProducer())
    event = sample_event(EventType.ORDER_CONFIRMED)
    assert await loop.handle(_message(event, offset=0)) is Outcome.PROCESSED  # type: ignore[arg-type]
    # Redelivered (e.g. offsets reset, or a crash before the offset commit).
    assert await loop.handle(_message(event, offset=1)) is Outcome.PROCESSED  # type: ignore[arg-type]
    assert db.effects == ["notify evt-1"]
    assert db.processed["evt-1"].consumer == "notification-service"
    assert consumer.committed == [0, 1]


async def test_concurrent_duplicate_commit_is_swallowed() -> None:
    db = FakeDb()
    db.commit_conflict = True
    await _idempotent(db)(sample_event(EventType.ORDER_CONFIRMED))
    assert db.effects == []


async def test_after_commit_runs_once_and_its_failure_is_contained() -> None:
    db = FakeDb()
    ran: list[str] = []

    async def handler(session: Any, event: DomainEvent) -> Any:
        session.add("effect")

        async def after() -> None:
            ran.append(f"committed={db.effects == ['effect']}")
            raise RuntimeError("rabbitmq down")

        return after

    process = idempotent(lambda: FakeSession(db), Processed, "c", handler)
    event = sample_event(EventType.ORDER_CONFIRMED)
    await process(event)  # the failing action doesn't fail the event
    await process(event)  # duplicate: no second action
    assert ran == ["committed=True"]


async def test_other_integrity_errors_are_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def commit_fails(self: FakeSession) -> None:
        raise IntegrityError("insert", {}, Exception("some other constraint"))

    monkeypatch.setattr(FakeSession, "commit", commit_fails)
    with pytest.raises(IntegrityError):
        await _idempotent(FakeDb())(sample_event(EventType.ORDER_CONFIRMED))
