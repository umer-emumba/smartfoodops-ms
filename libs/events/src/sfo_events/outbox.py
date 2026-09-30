from datetime import UTC, datetime
from typing import Any

from sfo_common import new_id
from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from sfo_events.schemas import EventType


class OutboxEventMixin:
    """Mix into each service's own `outbox_events` table (bound to that
    service's own declarative Base — no shared table across services, that
    would violate database-per-service). The business-row write and this
    row's write happen in the *same* DB transaction — that's what makes the
    outbox pattern reliable. A Celery-beat task (added Week 3) polls
    `dispatched_at IS NULL` rows and publishes them to Kafka.
    See docs/technical-documentation.md for the full rationale.
    """

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    aggregate_id: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def new_outbox_row[T](
    model_cls: type[T],
    *,
    aggregate_id: str,
    event_type: EventType,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> T:
    """`session.add(new_outbox_row(MyOutboxEvent, ...))` inside the same
    transaction as the business-entity write."""
    return model_cls(
        id=new_id(),
        aggregate_id=aggregate_id,
        event_type=event_type.value,
        payload=payload,
        correlation_id=correlation_id,
    )  # type: ignore[call-arg]
