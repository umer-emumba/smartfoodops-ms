"""The generic consumer loop (week3-plan W3-D4, W3-D5, W3-D6):

poll → deserialize → process(event) → commit offset

- `process` is usually `sfo_events.idempotency.idempotent(...)`, so the
  effect and the `processed_events` row commit together before the offset
  does. A crash in between redelivers the event, which is then a no-op.
- A handler failure is retried in-process with backoff; after the retries
  the message goes to `<topic>.dlq` with error headers, then its offset is
  committed, so one poison message can't block its partition.
- A message that can't be deserialized or validated goes to the DLQ at once
  (retrying won't fix it). An event type the consumer doesn't know (a newer
  producer) is skipped.
- An *outage* is not poison: when Postgres, Schema Registry or the network
  is unreachable (`is_transient`), nothing is dead-lettered or committed.
  The consumer rewinds to the message and tries again with backoff (capped)
  until the dependency is back. Otherwise every event during a database
  restart would land in the DLQ.
- If the DLQ publish or the offset commit fails, the same rewind applies:
  the message is redelivered, never lost.

Where it runs (two modes, same `ConsumerLoop`; picked by each service's
`kafka_consumer_mode` setting):
- Embedded (`running_in_background`): inside the API process, as an asyncio
  task on the API's own event loop thread. It shares that thread with the
  HTTP request handlers, so handlers (`process`) must not block it.
- Standalone (`run_standalone`): its own process (`python -m app.consumer`),
  no API at all. Its event loop runs the consumer on the main thread.

Which thread does what (in both modes):
- Event loop thread: the loop logic in `run`, deserialize, the handler
  (`process`, its async DB/Mongo writes), the retry sleeps, and — briefly
  blocking — the offset commit, the seek on rewind, and the DLQ produce+flush.
- Worker threads (`asyncio.to_thread`): only the blocking Kafka calls —
  `poll` (waits up to `poll_timeout` for a message) and `_record_lag`
  (asks the broker for offsets). These would otherwise freeze the event loop.
- librdkafka's own internal threads (inside confluent_kafka) handle
  fetching, heartbeats and group membership; we don't manage them.
"""

import asyncio
import logging
import signal
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, Protocol

from confluent_kafka import KafkaError, Message, TopicPartition
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from sfo_events import metrics
from sfo_events.kafka import Headers, dlq_headers, header_value
from sfo_events.schemas import DomainEvent, EventType, dlq_topic
from sfo_events.tracing import process_span

logger = logging.getLogger(__name__)

_LAG_SAMPLE_SECONDS = 15.0

Process = Callable[[DomainEvent], Awaitable[None]]
# For topics whose messages aren't events to deserialize — the DLQs.
ProcessRaw = Callable[[Message], Awaitable[None]]
Deserialize = Callable[[Message], dict[str, Any]]


class ConsumerLike(Protocol):
    def poll(self, timeout: float) -> Message | None: ...
    def commit(self, *, message: Message, asynchronous: bool) -> Any: ...
    def seek(self, partition: TopicPartition) -> None: ...


class ProducerLike(Protocol):
    def produce(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        headers: Headers | None = ...,
        on_delivery: Callable[[KafkaError | None, Message], None] | None = ...,
    ) -> None: ...
    def flush(self, timeout: float) -> int: ...


class Outcome(StrEnum):
    PROCESSED = "processed"
    SKIPPED = "skipped"
    DEAD_LETTERED = "dead_lettered"


class DeadLetterError(RuntimeError):
    """The DLQ publish failed — the offset is left uncommitted."""


# Exception classes (by name, anywhere in the MRO) that mean "couldn't reach
# a dependency". By name because the registry client raises errors of a
# vendored httpx fork, not importable as `httpx`.
_TRANSIENT_NAMES = {"TransportError", "KafkaException"}
# ...except these, which subclass KafkaException but mean a bad message
# (bytes not in the Schema Registry wire format, say). An outage behind one
# is still found, further down the `__cause__`/`__context__` chain.
_BAD_MESSAGE_NAMES = {"SerializationError"}


def is_transient(exc: BaseException) -> bool:
    """True for an outage (worth waiting out), False for a bad message or a
    bug. Follows `__cause__`/`__context__`, since libraries wrap errors."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError | TimeoutError | PoolTimeoutError):
            return True
        if isinstance(current, DBAPIError) and (
            current.connection_invalidated
            or type(current).__name__ in ("OperationalError", "InterfaceError")
        ):
            return True
        names = {cls.__name__ for cls in type(current).__mro__}
        if names & _TRANSIENT_NAMES and not names & _BAD_MESSAGE_NAMES:
            return True
        status = getattr(current, "http_status_code", None)
        if isinstance(status, int) and (status >= 500 or status < 0):
            return True  # Schema Registry unavailable
        current = current.__cause__ or current.__context__
    return False


def parse_event(data: dict[str, Any]) -> DomainEvent | None:
    """None for an event type this code doesn't know; raises if a known
    event is malformed."""
    if data.get("event_type") not in EventType.__members__.values():
        return None
    event = DomainEvent.model_validate(data)
    event.typed_payload()
    return event


def _header(msg: Message, name: str) -> str | None:
    return header_value(msg.headers(), name)


class ConsumerLoop:
    def __init__(
        self,
        *,
        consumer: ConsumerLike,
        deserialize: Deserialize,
        process: Process,
        dlq_producer: ProducerLike,
        group: str,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        poll_timeout: float = 1.0,
        dlq_flush_timeout: float = 10.0,
        max_outage_backoff_seconds: float = 30.0,
        raw_topics: frozenset[str] = frozenset(),
        process_raw: ProcessRaw | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Messages from `raw_topics` skip deserialization and go to
        `process_raw` as they are (e.g. dead letters, which by definition may
        not deserialize)."""
        if raw_topics and process_raw is None:
            raise ValueError("raw_topics needs process_raw")
        self._raw_topics = raw_topics
        self._process_raw = process_raw
        self._consumer = consumer
        self._deserialize = deserialize
        self._process = process
        self._dlq = dlq_producer
        self._group = group
        self._retries = retries
        self._backoff = backoff_seconds
        self._poll_timeout = poll_timeout
        self._dlq_flush_timeout = dlq_flush_timeout
        self._max_outage_backoff = max_outage_backoff_seconds
        self._sleep = sleep

    async def run(self, stop: asyncio.Event) -> None:
        """The consumer's main loop, one message at a time (so order within a
        partition is kept). Until `stop` is set. The blocking poll runs in a worker thread so
        the event loop stays free. A message that couldn't be finished
        (outage, failed DLQ publish or commit) is rewound to and retried
        after a backoff that grows to `max_outage_backoff_seconds`."""
        delay = self._backoff
        next_lag_sample = 0.0
        while not stop.is_set():
            # Worker thread: poll blocks while waiting for a message.
            msg = await asyncio.to_thread(self._consumer.poll, self._poll_timeout)
            now = time.monotonic()
            if now >= next_lag_sample:
                next_lag_sample = now + _LAG_SAMPLE_SECONDS
                # Worker thread too: this one calls the broker.
                await asyncio.to_thread(self._record_lag)
            if msg is None:
                continue
            try:
                await self.handle(msg)
                delay = self._backoff
            except Exception as exc:
                logger.warning(
                    "Couldn't finish %s@%s/%s, retrying in %.1fs: %r",
                    msg.topic(),
                    msg.partition(),
                    msg.offset(),
                    delay,
                    exc,
                )
                self._rewind(msg)
                metrics.EVENT_RETRIES.labels(self._group, msg.topic() or "", "outage").inc()
                await self._sleep(delay)
                delay = min(delay * 2, self._max_outage_backoff)

    async def handle(self, msg: Message) -> Outcome | None:
        """One message; None if it was a Kafka error rather than a record."""
        error = msg.error()
        if error is not None:
            if error.code() != KafkaError._PARTITION_EOF:
                logger.warning("Kafka consumer error: %s", error)
            return None
        with process_span(
            msg.topic() or "", msg.headers(), self._group, _header(msg, "event_type")
        ):
            return await self._handle_record(msg)

    async def _handle_record(self, msg: Message) -> Outcome:
        if msg.topic() in self._raw_topics:
            return await self._handle_raw(msg)
        try:
            event = parse_event(self._deserialize(msg))
        except Exception as exc:
            if is_transient(exc):
                raise
            logger.exception("Undeserializable message → DLQ")
            self._dead_letter(msg, exc, attempts=0)
            return self._commit(msg, Outcome.DEAD_LETTERED)
        if event is None:
            return self._commit(msg, Outcome.SKIPPED)

        delay = self._backoff
        for attempt in range(1, self._retries + 2):
            try:
                await self._process(event)
                return self._commit(msg, Outcome.PROCESSED)
            except Exception as exc:
                if is_transient(exc):
                    raise
                if attempt > self._retries:
                    logger.exception(
                        "Event %s (%s) failed %d times → DLQ",
                        event.event_id,
                        event.event_type,
                        attempt,
                    )
                    self._dead_letter(msg, exc, attempts=attempt)
                    return self._commit(msg, Outcome.DEAD_LETTERED)
                logger.warning(
                    "Event %s (%s) failed (attempt %d), retrying in %.1fs: %s",
                    event.event_id,
                    event.event_type,
                    attempt,
                    delay,
                    exc,
                )
                metrics.EVENT_RETRIES.labels(self._group, msg.topic() or "", "handler").inc()
                await self._sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    async def _handle_raw(self, msg: Message) -> Outcome:
        """No DLQ for these (no dead letters of dead letters): an outage is
        retried like any other, anything else is logged and skipped."""
        assert self._process_raw is not None
        try:
            await self._process_raw(msg)
        except Exception as exc:
            if is_transient(exc):
                raise
            logger.exception("Couldn't process %s@%s — skipped", msg.topic(), msg.offset())
            return self._commit(msg, Outcome.SKIPPED)
        return self._commit(msg, Outcome.PROCESSED)

    def _record_lag(self) -> None:
        """Messages behind per assigned partition: end offset − committed
        offset, as `kafka-consumer-groups` reports it. Not the consumer's
        position: that stays unset until it reads from the partition, so a
        consumer restarted with nothing new to read would look behind by the
        whole log. Also creates the dead-lettered series at 0 for each topic,
        so the first dead letter is an increase an alert can see. Blocking
        (asks the broker) — run in a thread. Fakes without these methods
        (tests) just skip it."""
        assignment = getattr(self._consumer, "assignment", None)
        if assignment is None:
            return
        try:
            partitions = assignment()
            if not partitions:
                return
            for tp in self._consumer.committed(partitions, timeout=2):  # type: ignore[attr-defined]
                low, end = self._consumer.get_watermark_offsets(tp, timeout=2)  # type: ignore[attr-defined]
                # Nothing committed yet: everything still in the log is unread.
                lag = end - (tp.offset if tp.offset >= 0 else low)
                metrics.CONSUMER_LAG.labels(self._group, tp.topic, str(tp.partition)).set(
                    max(lag, 0)
                )
                if tp.topic not in self._raw_topics:
                    metrics.EVENTS_CONSUMED.labels(
                        self._group, tp.topic, Outcome.DEAD_LETTERED.value
                    )
        except Exception:
            logger.debug("Consumer lag sample failed", exc_info=True)

    def _rewind(self, msg: Message) -> None:
        """Next poll returns `msg` again (the consumer's position had moved
        past it; its committed offset never did)."""
        topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
        assert topic is not None and partition is not None and offset is not None
        self._consumer.seek(TopicPartition(topic, partition, offset))

    def _commit(self, msg: Message, outcome: Outcome) -> Outcome:
        # Synchronous commit, on the event loop thread: a short block, but the
        # offset is saved before we move on, which is what makes redelivery safe.
        self._consumer.commit(message=msg, asynchronous=False)
        metrics.EVENTS_CONSUMED.labels(self._group, msg.topic() or "", outcome.value).inc()
        return outcome

    def _dead_letter(self, msg: Message, error: BaseException, *, attempts: int) -> None:
        """Copies the failed message to its `.dlq` topic and waits (flush) for
        Kafka to confirm. Runs on the event loop thread; rare, so the block
        is accepted."""
        failures: list[KafkaError] = []

        def on_delivery(err: KafkaError | None, _: Message) -> None:
            if err is not None:
                failures.append(err)

        self._dlq.produce(
            dlq_topic(msg.topic() or ""),
            value=msg.value(),
            key=msg.key(),
            headers=dlq_headers(msg, group=self._group, error=error, attempts=attempts),
            on_delivery=on_delivery,
        )
        if self._dlq.flush(self._dlq_flush_timeout) > 0 or failures:
            raise DeadLetterError(f"DLQ publish failed for {msg.topic()}@{msg.offset()}")


@asynccontextmanager
async def running_in_background(
    loop: ConsumerLoop, close: Callable[[], None]
) -> AsyncGenerator[None, None]:
    """Embedded mode (W3-D6): runs `loop` as a task of the API's event loop
    (same process and thread as the HTTP handlers) for the duration of the
    lifespan, then stops it and closes the Kafka
    consumer (leaving the group, so its partitions move on at once)."""
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop), name="kafka-consumer")
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            logger.exception("Kafka consumer didn't stop cleanly")
        close()


def run_standalone(loop: ConsumerLoop, close: Callable[[], None]) -> None:
    """Standalone mode: `python -m app.consumer` — the consumer as its own
    process, with no API and no HTTP server. "Standalone" = not embedded in
    the API. It runs until SIGTERM/SIGINT (docker stop, Ctrl-C), which set
    `stop` so the loop finishes its current message and `close` leaves the
    consumer group cleanly. Same loop and handlers as embedded mode; only
    where it runs differs (lets you scale consumers separately from the API).
    """

    async def main() -> None:
        stop = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            event_loop.add_signal_handler(sig, stop.set)
        try:
            await loop.run(stop)
        finally:
            close()

    asyncio.run(main())
