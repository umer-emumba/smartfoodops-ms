"""The DLQ redrive (sfo_events.redrive) with a fake consumer and producer."""

from collections.abc import Callable
from typing import Any

import pytest
from confluent_kafka import KafkaError, TopicPartition
from sfo_events.kafka import Headers, header_value
from sfo_events.redrive import HEADER_REDRIVEN, republish_for, run

DLQ = "order-events.dlq"


class FakeMessage:
    def __init__(
        self,
        offset: int,
        *,
        group: str = "notification-service",
        origin: str | None = "order-events",
        partition: int = 0,
        topic: str = DLQ,
    ) -> None:
        self._offset, self._partition, self._topic = offset, partition, topic
        self._headers: Headers = [("event_id", f"e-{offset}".encode()), ("event_type", b"X")]
        if origin is not None:
            self._headers += [
                ("dlq.original.topic", origin.encode()),
                ("dlq.consumer.group", group.encode()),
                ("dlq.error.type", b"ValueError"),
                ("dlq.attempts", b"4"),
            ]

    def error(self) -> KafkaError | None:
        return None

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def key(self) -> bytes:
        return b"order-1"

    def value(self) -> bytes:
        return f"payload-{self._offset}".encode()

    def headers(self) -> Headers:
        return self._headers


class FakeConsumer:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages
        self.assigned: list[TopicPartition] = []

    def assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = partitions
        starts = {tp.partition: tp.offset for tp in partitions}
        self.messages = [
            m
            for m in self.messages
            if m.partition() in starts and m.offset() >= starts[m.partition()]
        ]

    def poll(self, timeout: float) -> Any:
        return self.messages.pop(0) if self.messages else None


class FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.produced: list[dict[str, Any]] = []
        self._fail = fail
        self._callbacks: list[Callable[..., None]] = []

    def produce(self, topic: str, **kwargs: Any) -> None:
        self.produced.append({"topic": topic, **kwargs})
        self._callbacks.append(kwargs["on_delivery"])

    def flush(self, timeout: float) -> int:
        for callback in self._callbacks:
            callback(KafkaError(KafkaError._MSG_TIMED_OUT) if self._fail else None, None)
        return 0


def test_republish_restores_topic_key_value_and_original_headers() -> None:
    target = republish_for(FakeMessage(7), group=None)  # type: ignore[arg-type]
    assert target is not None
    assert (target.topic, target.key, target.value) == ("order-events", b"order-1", b"payload-7")
    assert not [k for k, _ in target.headers if k.startswith("dlq.") and k != HEADER_REDRIVEN]
    assert header_value(target.headers, "event_id") == "e-7"
    assert header_value(target.headers, HEADER_REDRIVEN) == f"{DLQ}[0]@7"


def test_republish_skips_other_groups_and_non_dead_letters() -> None:
    assert republish_for(FakeMessage(1, group="analytics-service"), "notification-service") is None  # type: ignore[arg-type]
    assert republish_for(FakeMessage(1, origin=None), None) is None  # type: ignore[arg-type]
    # A header naming another topic than this DLQ's is not trusted.
    assert republish_for(FakeMessage(1, origin="payment-events"), None) is None  # type: ignore[arg-type]


def test_run_replays_the_range_for_one_group() -> None:
    messages = [
        FakeMessage(0),
        FakeMessage(1, group="analytics-service"),
        FakeMessage(2),
        FakeMessage(3),  # at the end offset: dead-lettered after the run began
    ]
    producer = FakeProducer()
    report = run(
        FakeConsumer(messages),
        producer,
        {TopicPartition(DLQ, 0, 0): 3},
        group="notification-service",
        out=lambda _: None,
    )
    assert [p["value"] for p in producer.produced] == [b"payload-0", b"payload-2"]
    assert {p["topic"] for p in producer.produced} == {"order-events"}
    assert (report.scanned, report.republished, report.skipped) == (3, 2, 1)
    assert report.next_offsets == {0: 3}


def test_dry_run_publishes_nothing() -> None:
    producer = FakeProducer()
    report = run(
        FakeConsumer([FakeMessage(0), FakeMessage(1)]),
        producer,
        {TopicPartition(DLQ, 0, 0): 2},
        dry_run=True,
        out=lambda _: None,
    )
    assert producer.produced == [] and report.republished == 2


def test_limit_stops_early_and_reports_where_to_continue() -> None:
    report = run(
        FakeConsumer([FakeMessage(i) for i in range(5)]),
        FakeProducer(),
        {TopicPartition(DLQ, 0, 0): 5},
        limit=2,
        out=lambda _: None,
    )
    assert report.republished == 2 and report.next_offsets == {0: 2}


def test_empty_range_reads_nothing() -> None:
    consumer = FakeConsumer([FakeMessage(0)])
    report = run(consumer, FakeProducer(), {TopicPartition(DLQ, 0, 4): 4}, out=lambda _: None)
    assert consumer.assigned == [] and report.scanned == 0


def test_unconfirmed_republish_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="failed"):
        run(
            FakeConsumer([FakeMessage(0)]),
            FakeProducer(fail=True),
            {TopicPartition(DLQ, 0, 0): 1},
            out=lambda _: None,
        )
