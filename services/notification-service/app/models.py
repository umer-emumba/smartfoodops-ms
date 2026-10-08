from datetime import UTC, datetime

from sfo_common import new_id
from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Populated by the Kafka consumer + Celery dispatch task added in Week 3
# (docs/PROGRESS.md). Week 1 just establishes the schema and a read path so
# a customer can view their notification history once it exists.


class Notification(Base):
    __tablename__ = "notifications"
    # Keyset pagination of a user's own notifications (sfo_common.pagination);
    # also serves plain user_id lookups.
    __table_args__ = (
        Index("ix_notifications_user_id_created_at_id", "user_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String)  # opaque ref to user-service
    # So a customer can click through from a notification to what it's about.
    related_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String)  # order_update | delivery_update | cancellation ...
    channel: Mapped[str] = mapped_column(String, default="in_app")  # in_app | email | sms | push
    content: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | sent | failed
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
