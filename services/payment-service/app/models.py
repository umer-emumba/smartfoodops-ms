from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# status: authorized | captured | refunded | failed — full authorize/capture/
# refund logic lands in Week 2 (docs/PROGRESS.md). Week 1 just establishes the
# schema and a read path.


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # opaque ref to order-service
    # opaque ref to user-service — needed to authorize GET /payments/by-order/{id}
    # against the caller without a cross-service call (docs/architecture.md).
    customer_id: Mapped[str] = mapped_column(String, index=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String)
    payment_method: Mapped[str] = mapped_column(String, default="card_simulated")
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    refunded_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(Base, OutboxEventMixin):
    __tablename__ = "outbox_events"
