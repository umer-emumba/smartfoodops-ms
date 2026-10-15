from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import ProcessedEventMixin
from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Written by the Kafka consumer (app/consumer.py) — one row per recipient per
# channel — and sent by the Celery worker (app/tasks.py), docs/week3-plan.md
# W3-D7. status: pending → sent | failed.


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # One row per event, recipient and channel: a replayed event can't
        # notify twice even if processed_events were bypassed.
        UniqueConstraint(
            "event_id", "user_id", "channel", name="uq_notifications_event_user_channel"
        ),
        # A user's inbox (in-app only), newest first — keyset pagination.
        Index(
            "ix_notifications_inbox",
            "user_id",
            "created_at",
            "id",
            postgresql_where=text("channel = 'in_app'"),
        ),
        # The sweep's scan for sends that never got queued.
        Index(
            "ix_notifications_pending", "created_at", postgresql_where=text("status = 'pending'")
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String)  # opaque ref to user-service
    recipient_role: Mapped[str] = mapped_column(String)  # customer | restaurant_admin | rider
    # The domain event that caused it.
    event_id: Mapped[str] = mapped_column(String)
    # So a customer can click through from a notification to what it's about.
    related_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String)  # app/templates.py
    channel: Mapped[str] = mapped_column(String, default="in_app")  # in_app | email | sms
    content: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | sent | failed
    # Failed send attempts so far; at notify_max_attempts the row is `failed`.
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessedEvent(Base, ProcessedEventMixin):
    """Events whose notifications are committed (week3-plan W3-D4)."""

    __tablename__ = "processed_events"
