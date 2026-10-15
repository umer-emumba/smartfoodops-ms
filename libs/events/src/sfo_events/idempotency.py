from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from sfo_events.schemas import DomainEvent

SessionHandler = Callable[[AsyncSession, DomainEvent], Awaitable[None]]


class ProcessedEventMixin:
    """Mix into a consuming service's own `processed_events` table (same
    database-per-service rule as OutboxEventMixin). A row means "this event's
    effect is committed" — written in the same transaction as the effect
    (week3-plan W3-D4), so a redelivered event is a no-op."""

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    consumer: Mapped[str] = mapped_column(String)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def idempotent(
    session_factory: Callable[[], Any],
    processed_model: type[Any],
    consumer: str,
    handler: SessionHandler,
) -> Callable[[DomainEvent], Awaitable[None]]:
    """Wraps `handler(session, event)` so its writes and the
    `processed_events` row commit together, once per event_id. The handler
    adds to the session and must not commit."""

    async def process(event: DomainEvent) -> None:
        async with session_factory() as session:
            if await session.get(processed_model, event.event_id) is not None:
                return
            await handler(session, event)
            session.add(processed_model(event_id=event.event_id, consumer=consumer))
            try:
                await session.commit()
            except IntegrityError:
                # Another delivery of the same event committed first — fine.
                # Anything else (a real constraint bug) is re-raised.
                await session.rollback()
                if await session.get(processed_model, event.event_id) is None:
                    raise

    return process
