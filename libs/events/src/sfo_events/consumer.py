"""The generic consumer loop (week3-plan W3-D4, W3-D5):

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
- If the DLQ publish itself fails the offset is not committed: the message
  is redelivered rather than lost.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from confluent_kafka import KafkaError, Message

from sfo_events.kafka import Headers, dlq_headers
from sfo_events.schemas import DomainEvent, EventType, dlq_topic

logger = logging.getLogger(__name__)

Process = Callable[[DomainEvent], Awaitable[None]]
Deserialize = Callable[[Message], dict[str, Any]]


class ConsumerLike(Protocol):
    def poll(self, timeout: float) -> Message | None: ...
    def commit(self, *, message: Message, asynchronous: bool) -> Any: ...


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


def parse_event(data: dict[str, Any]) -> DomainEvent | None:
    """None for an event type this code doesn't know; raises if a known
    event is malformed."""
    if data.get("event_type") not in EventType.__members__.values():
        return None
    event = DomainEvent.model_validate(data)
    event.typed_payload()
    return event


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._consumer = consumer
        self._deserialize = deserialize
        self._process = process
        self._dlq = dlq_producer
        self._group = group
        self._retries = retries
        self._backoff = backoff_seconds
        self._poll_timeout = poll_timeout
        self._dlq_flush_timeout = dlq_flush_timeout
        self._sleep = sleep

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop` is set. The blocking poll runs in a worker thread so
        the event loop stays free."""
        while not stop.is_set():
            msg = await asyncio.to_thread(self._consumer.poll, self._poll_timeout)
            if msg is not None:
                await self.handle(msg)

    async def handle(self, msg: Message) -> Outcome | None:
        """One message; None if it was a Kafka error rather than a record."""
        error = msg.error()
        if error is not None:
            if error.code() != KafkaError._PARTITION_EOF:
                logger.warning("Kafka consumer error: %s", error)
            return None
        try:
            event = parse_event(self._deserialize(msg))
        except Exception as exc:
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
                await self._sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def _commit(self, msg: Message, outcome: Outcome) -> Outcome:
        self._consumer.commit(message=msg, asynchronous=False)
        return outcome

    def _dead_letter(self, msg: Message, error: BaseException, *, attempts: int) -> None:
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
