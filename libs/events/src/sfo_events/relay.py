"""The outbox relay (docs/week3-plan.md W3-D1, W3-D2): publishes a service's
undispatched outbox rows to Kafka. Needs the `relay` extra.

Each tick, in one DB transaction:

1. `pg_try_advisory_xact_lock` — only one relay per service at a time, even
   with several worker replicas; a tick that can't get it does nothing.
   (Not `SKIP LOCKED`: two relays splitting rows would split one order's
   events between them and break their order.)
2. The oldest undispatched rows, `ORDER BY created_at, id`.
3. Produce them all (key = aggregate_id, so one aggregate's events land on
   one partition, in order), then `flush()`.
4. Rows Kafka acknowledged get `dispatched_at`; the rest get
   `publish_attempts + 1` and `last_error` and are retried next tick.

At-least-once: a crash after Kafka acknowledged but before the commit
re-publishes those rows next tick; consumers drop the duplicates by
`event_id` (W3-D4).

Ordering: once a row fails to serialize, later rows of the same aggregate
in the batch aren't sent, so they can't overtake it. Payloads are
validated when written (`new_outbox_row`), so a row that can never be sent
should not exist; if one does, it holds back only its own aggregate, and the
outbox-age metric shows it (Week 3 step 5).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from confluent_kafka import KafkaError, Message, Producer
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sfo_events.kafka import EventSerializer, event_headers, make_producer, make_registry_client
from sfo_events.schemas import EVENT_TOPICS, DomainEvent, EventType
from sfo_events.tracing import current_traceparent, publish_span, quiet

logger = logging.getLogger(__name__)

# Fixed key: every service has its own database, so it can't collide
# (sfo_common.db's migration lock is 727_001).
RELAY_LOCK_KEY = 727_002
_PURGE_CHUNK = 1000


@dataclass
class RelayResult:
    published: int = 0
    failed: int = 0
    # Another relay of this service held the lock.
    locked_out: bool = False


def to_event(row: Any) -> DomainEvent:
    """The Kafka envelope for an outbox row; `occurred_at` is when the row
    was written (W3-D8)."""
    return DomainEvent(
        event_id=row.id,
        event_type=EventType(row.event_type),
        aggregate_id=row.aggregate_id,
        occurred_at=row.created_at,
        correlation_id=row.correlation_id,
        payload=row.payload,
    )


class OutboxRelay:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        outbox_model: type[Any],
        bootstrap_servers: str,
        schema_registry_url: str,
        batch_size: int = 200,
        delivery_timeout_seconds: float = 5.0,
        producer: Producer | None = None,
        serializer: EventSerializer | None = None,
    ) -> None:
        """`producer`/`serializer` are normally built on the first tick;
        tests pass fakes."""
        self._session_factory = session_factory
        self._model = outbox_model
        self._bootstrap_servers = bootstrap_servers
        self._schema_registry_url = schema_registry_url
        self._batch_size = batch_size
        self._delivery_timeout = delivery_timeout_seconds
        self._producer = producer
        self._serializer = serializer

    def _connect(self) -> tuple[Producer, EventSerializer]:
        """Created on the first tick, not at import: a registry that's down
        fails that tick (retried next second), not the worker's start-up."""
        if self._producer is None or self._serializer is None:
            serializer = EventSerializer(make_registry_client(self._schema_registry_url))
            serializer.register_all()
            # Kafka gives up on a message within the delivery timeout, so
            # every delivery callback has fired by the time flush() returns —
            # each row's outcome is known before the transaction commits
            # (which also keeps the transaction well under Postgres'
            # idle-in-transaction timeout, sfo_common.db).
            self._producer = make_producer(
                self._bootstrap_servers,
                **{"message.timeout.ms": int(self._delivery_timeout * 1000)},
            )
            self._serializer = serializer
        return self._producer, self._serializer

    async def relay_once(self) -> RelayResult:
        """One tick. Its SQL isn't traced (`quiet`) — a poll every second
        would start a trace each time; each published row gets a span in
        the trace that wrote it instead."""
        with quiet():
            return await self._relay_once()

    async def _relay_once(self) -> RelayResult:
        producer, serializer = self._connect()
        model = self._model
        async with self._session_factory() as session:
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": RELAY_LOCK_KEY}
            )
            if not locked:
                return RelayResult(locked_out=True)
            rows = (
                (
                    await session.execute(
                        select(model)
                        .where(model.dispatched_at.is_(None))
                        .order_by(model.created_at, model.id)
                        .limit(self._batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return RelayResult()

            errors: dict[str, str] = {}
            acked: set[str] = set()
            blocked: set[str] = set()

            def on_delivery(row_id: str) -> Callable[[KafkaError | None, Message], None]:
                def callback(err: KafkaError | None, _: Message) -> None:
                    if err is None:
                        acked.add(row_id)
                    else:
                        errors[row_id] = f"kafka: {err}"

                return callback

            for row in rows:
                if row.aggregate_id in blocked:
                    continue
                try:
                    event = to_event(row)
                    value = serializer.serialize(event)
                except Exception as exc:
                    errors[row.id] = f"serialize: {exc}"
                    blocked.add(row.aggregate_id)
                    continue
                topic = EVENT_TOPICS[event.event_type]
                with publish_span(topic, row.event_type, row.id, row.trace_context):
                    traceparent = current_traceparent()
                    headers = event_headers(event)
                    if traceparent:
                        headers.append(("traceparent", traceparent.encode()))
                    producer.produce(
                        topic,
                        key=row.aggregate_id.encode(),
                        value=value,
                        headers=headers,
                        on_delivery=on_delivery(row.id),
                    )
            # Blocking; callbacks run inside flush().
            await asyncio.to_thread(producer.flush, self._delivery_timeout + 2)

            now = datetime.now(UTC)
            result = RelayResult()
            for row in rows:
                if row.id in acked:
                    row.dispatched_at = now
                    row.last_error = None
                    result.published += 1
                elif row.id in errors:
                    row.publish_attempts += 1
                    row.last_error = errors[row.id][:1000]
                    result.failed += 1
            await session.commit()
        if result.failed:
            logger.warning(
                "Outbox relay: %d published, %d failed (first error: %s)",
                result.published,
                result.failed,
                next(iter(errors.values())),
            )
        return result

    async def purge_dispatched(self, older_than: timedelta) -> int:
        """Deletes rows dispatched before `older_than` ago, in chunks so no
        single statement holds locks for long. Returns how many."""
        model = self._model
        cutoff = datetime.now(UTC) - older_than
        total = 0
        while True:
            async with self._session_factory() as session:
                ids = select(model.id).where(model.dispatched_at < cutoff).limit(_PURGE_CHUNK)
                result = await session.execute(delete(model).where(model.id.in_(ids)))
                await session.commit()
            deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]
            total += deleted
            if deleted < _PURGE_CHUNK:
                return total


def register_relay_tasks(
    app: Any, relay: OutboxRelay, *, interval_seconds: float, retention_days: int
) -> None:
    """The relay and purge tasks on `app` (a sfo_common.celery app), plus
    their beat schedule. A relay tick expires after one interval, so ticks
    never pile up in RabbitMQ while a slow one (Kafka down) runs."""
    from sfo_common.celery import run_async

    prefix = app.main

    @app.task(name=f"{prefix}.relay_outbox")  # type: ignore[untyped-decorator]
    def relay_outbox() -> None:
        result = run_async(relay.relay_once())
        if result.published:
            logger.info("Outbox relay: published %d", result.published)

    @app.task(name=f"{prefix}.purge_outbox")  # type: ignore[untyped-decorator]
    def purge_outbox() -> None:
        deleted = run_async(relay.purge_dispatched(timedelta(days=retention_days)))
        if deleted:
            logger.info(
                "Outbox purge: deleted %d rows dispatched > %d days ago", deleted, retention_days
            )

    app.conf.beat_schedule = {
        "relay-outbox": {
            "task": f"{prefix}.relay_outbox",
            "schedule": interval_seconds,
            "options": {"expires": interval_seconds},
        },
        "purge-outbox": {
            "task": f"{prefix}.purge_outbox",
            "schedule": 3600.0,
            "options": {"expires": 3600.0},
        },
    }
