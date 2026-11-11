"""Replays dead letters (W3-D5's DLQ) to the topic they came from, once the
bug or outage that put them there is fixed.

    docker compose exec analytics-service python -m sfo_events.redrive \\
        --topic order-events.dlq --group notification-service --since 2026-11-10T09:00 --dry-run

Any service image has this module and can reach Kafka (the broker is only
on the compose network). Drop `--dry-run` to republish.

- Each dead letter goes back to `dlq.original.topic` with its original key
  (same partition, so per-order order is kept among new events), value
  bytes (still valid against Schema Registry) and headers. The `dlq.*`
  headers are dropped and `dlq.redriven` names the DLQ position it came from.
- Every consumer group of that topic gets the event again, not only the
  group that failed. That's safe: each consumer is duplicate-safe
  (notification-service's processed_events; analytics' and tracking's
  writes are idempotent by construction), so the groups that already
  processed it change nothing. It arrives after newer events for the same
  order; those consumers ignore stale ones.
- `--group` replays only the dead letters one consumer group wrote (the DLQ
  is shared by every group of the topic).
- The range is chosen per run (`--since`, `--from-offset`, default: the whole
  retained DLQ, 7 days) and ends at the DLQ's end when the run starts, so a
  message that fails again and is dead-lettered again isn't picked up by the
  same run. Nothing is committed: no position is remembered between runs,
  and skipping another group's dead letters never hides them from a later
  run. Running the same range twice replays it twice — harmless, see above.
- Messages that couldn't be deserialized (`dlq.attempts` 0) fail the same
  way again unless their producer was fixed; they just return to the DLQ.
"""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from confluent_kafka import OFFSET_END, KafkaError, KafkaException, Message, TopicPartition

from sfo_events.kafka import (
    HEADER_DLQ_ATTEMPTS,
    HEADER_DLQ_ERROR_TYPE,
    HEADER_DLQ_GROUP,
    HEADER_DLQ_TOPIC,
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    Headers,
    header_items,
    header_value,
    make_consumer,
    make_producer,
)

HEADER_REDRIVEN = "dlq.redriven"
_DLQ_SUFFIX = ".dlq"
# Polls in a row with nothing before giving up on reaching the end offsets.
_MAX_IDLE_POLLS = 10


class RedriveConsumer(Protocol):
    def assign(self, partitions: list[TopicPartition]) -> None: ...
    def poll(self, timeout: float) -> Message | None: ...


class RedriveProducer(Protocol):
    def produce(
        self,
        topic: str,
        *,
        value: bytes | None = ...,
        key: bytes | None = ...,
        headers: Headers | None = ...,
        on_delivery: Callable[[KafkaError | None, Message], None] | None = ...,
    ) -> None: ...
    def flush(self, timeout: float) -> int: ...


@dataclass(frozen=True)
class Republish:
    topic: str
    key: bytes | None
    value: bytes | None
    headers: Headers


@dataclass
class Report:
    scanned: int = 0
    republished: int = 0
    skipped: int = 0
    # Per partition, the offset to start the next run from.
    next_offsets: dict[int, int] = field(default_factory=dict)


def republish_for(msg: Message, group: str | None) -> Republish | None:
    """What to publish for a dead letter, or None to skip it (another
    group's, or not a dead letter of this DLQ)."""
    headers = msg.headers()
    dlq = msg.topic() or ""
    target = header_value(headers, HEADER_DLQ_TOPIC)
    if not target or target + _DLQ_SUFFIX != dlq:
        return None
    if group is not None and header_value(headers, HEADER_DLQ_GROUP) != group:
        return None
    kept: Headers = [(k, v) for k, v in header_items(headers) if not k.startswith("dlq.")]
    kept.append((HEADER_REDRIVEN, f"{dlq}[{msg.partition()}]@{msg.offset()}".encode()))
    return Republish(topic=target, key=msg.key(), value=msg.value(), headers=kept)


def describe(msg: Message) -> str:
    headers = msg.headers()
    return (
        f"{msg.topic()}[{msg.partition()}]@{msg.offset()}"
        f" → {header_value(headers, HEADER_DLQ_TOPIC)}"
        f" ({header_value(headers, HEADER_EVENT_TYPE)} {header_value(headers, HEADER_EVENT_ID)},"
        f" group {header_value(headers, HEADER_DLQ_GROUP)},"
        f" {header_value(headers, HEADER_DLQ_ERROR_TYPE)},"
        f" attempts {header_value(headers, HEADER_DLQ_ATTEMPTS)})"
    )


def run(
    consumer: RedriveConsumer,
    producer: RedriveProducer,
    ranges: dict[TopicPartition, int],
    *,
    group: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    out: Callable[[str], None] = print,
    flush_timeout: float = 30.0,
) -> Report:
    """Replays each partition from its TopicPartition's offset up to (not
    including) the end offset it maps to. Raises if Kafka didn't confirm
    every republished message."""
    report = Report()
    remaining = {tp.partition: end for tp, end in ranges.items() if tp.offset < end}
    report.next_offsets = {tp.partition: tp.offset for tp in ranges}
    if not remaining:
        return report
    consumer.assign([tp for tp in ranges if tp.partition in remaining])
    failures: list[KafkaError] = []

    def on_delivery(err: KafkaError | None, _: Message) -> None:
        if err is not None:
            failures.append(err)

    idle = 0
    while remaining and (limit is None or report.republished < limit):
        msg = consumer.poll(1.0)
        if msg is None:
            idle += 1
            if idle >= _MAX_IDLE_POLLS:
                out(f"Stopped: nothing more to read from partitions {sorted(remaining)}")
                break
            continue
        idle = 0
        error = msg.error()
        if error is not None:
            if error.code() == KafkaError._PARTITION_EOF:
                remaining.pop(msg.partition() or 0, None)
                continue
            raise KafkaException(error)
        partition, offset = msg.partition(), msg.offset()
        assert partition is not None and offset is not None
        end = remaining.get(partition)
        if end is None or offset >= end:
            remaining.pop(partition, None)
            continue
        report.scanned += 1
        report.next_offsets[partition] = offset + 1
        if offset + 1 >= end:
            remaining.pop(partition)
        target = republish_for(msg, group)
        if target is None:
            report.skipped += 1
            continue
        out(("would replay " if dry_run else "replaying ") + describe(msg))
        report.republished += 1
        if not dry_run:
            producer.produce(
                target.topic,
                value=target.value,
                key=target.key,
                headers=target.headers,
                on_delivery=on_delivery,
            )
    if not dry_run and report.republished:
        unconfirmed = producer.flush(flush_timeout)
        if failures or unconfirmed:
            raise RuntimeError(
                f"{len(failures)} republish(es) failed, {unconfirmed} unconfirmed: {failures[:3]}"
            )
    return report


def plan_ranges(
    consumer: object,
    topic: str,
    *,
    partition: int | None,
    since: datetime | None,
    from_offset: int | None,
) -> dict[TopicPartition, int]:
    """Start offset (in the TopicPartition) → end offset, per partition, from
    the broker. The end is the DLQ's end now."""
    metadata = consumer.list_topics(topic, timeout=10)  # type: ignore[attr-defined]
    topic_meta = metadata.topics.get(topic)
    if topic_meta is None or topic_meta.error is not None:
        raise SystemExit(f"No such topic: {topic}")
    partitions = sorted(topic_meta.partitions)
    if partition is not None:
        if partition not in partitions:
            raise SystemExit(f"{topic} has no partition {partition}")
        partitions = [partition]
    ranges: dict[TopicPartition, int] = {}
    for p in partitions:
        low, high = consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10)  # type: ignore[attr-defined]
        start = low
        if since is not None:
            ms = int(since.timestamp() * 1000)
            [found] = consumer.offsets_for_times([TopicPartition(topic, p, ms)], timeout=10)  # type: ignore[attr-defined]
            start = high if found.offset in (-1, OFFSET_END) else found.offset
        elif from_offset is not None:
            start = from_offset
        ranges[TopicPartition(topic, p, max(start, low))] = high
    return ranges


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sfo_events.redrive",
        description="Replay dead letters from a .dlq topic to their original topic.",
    )
    parser.add_argument("--topic", required=True, help="the DLQ, e.g. order-events.dlq")
    parser.add_argument("--group", help="only dead letters this consumer group wrote")
    start = parser.add_mutually_exclusive_group()
    start.add_argument(
        "--since", type=_parse_since, help="ISO time (UTC if no zone): dead-lettered from then on"
    )
    start.add_argument("--from-offset", type=int, help="start offset (with --partition)")
    parser.add_argument("--partition", type=int, help="only this DLQ partition")
    parser.add_argument("--max", type=int, dest="limit", help="replay at most this many")
    parser.add_argument("--dry-run", action="store_true", help="list, don't republish")
    parser.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
    )
    args = parser.parse_args(argv)
    if not args.topic.endswith(_DLQ_SUFFIX):
        parser.error("--topic must be a .dlq topic")
    if args.from_offset is not None and args.partition is None:
        parser.error("--from-offset needs --partition (offsets differ per partition)")

    # A group.id is required by the client; nothing is ever committed under it.
    consumer = make_consumer(
        args.bootstrap_servers, "dlq-redrive", **{"enable.partition.eof": True}
    )
    producer = make_producer(args.bootstrap_servers)
    try:
        ranges = plan_ranges(
            consumer,
            args.topic,
            partition=args.partition,
            since=args.since,
            from_offset=args.from_offset,
        )
        report = run(
            consumer,
            producer,
            ranges,
            group=args.group,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    finally:
        consumer.close()
    verb = "would replay" if args.dry_run else "replayed"
    print(
        f"{report.scanned} scanned, {verb} {report.republished}, skipped {report.skipped}."
        f" Next run from offsets {report.next_offsets}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
