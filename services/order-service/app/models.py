from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Week 1: skeleton create/read only. Week 2 adds the Temporal saga that drives
# transitions through this state machine — see docs/architecture.md.
# created -> awaiting_restaurant_acceptance -> confirmed -> assigned -> picked_up -> delivered
# plus: rejected, cancelled
ORDER_STATUS_CREATED = "created"


class Order(Base):
    __tablename__ = "orders"
    # Scoped per customer, not global — the key is client-generated, so two
    # customers' clients may independently pick the same value.
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_orders_customer_idempotency_key"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    # Human-facing, sequential — distinct from the UUID primary key, for
    # customer support / receipts (docs/technical-documentation.md).
    order_number: Mapped[int] = mapped_column(Integer, Identity(start=1000), unique=True)
    customer_id: Mapped[str] = mapped_column(String, index=True)  # opaque ref to user-service
    restaurant_id: Mapped[str] = mapped_column(String, index=True)  # opaque ref to restaurant-service
    status: Mapped[str] = mapped_column(String, default=ORDER_STATUS_CREATED)

    # Idempotency-Key header from POST /v1/orders, plus a hash of the request
    # body — a retry with the same key and same body returns this order
    # instead of creating a duplicate; same key with a different body is a
    # 409. Nullable only so pre-existing dev rows survive the migration.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    # Delivery address is snapshotted at order time, not a live reference —
    # a customer's saved address (user-service) can change/be deleted later,
    # but the order must keep recording where it was actually delivered.
    delivery_address_text: Mapped[str] = mapped_column(String)
    delivery_lat: Mapped[float] = mapped_column(Float)
    delivery_lng: Mapped[float] = mapped_column(Float)
    special_instructions: Mapped[str | None] = mapped_column(String, nullable=True)

    # Pricing breakdown — PRD requires "pricing and discounts must be
    # applied"; Week 1 populates subtotal only, tax/fee/discount computation
    # is the saga's "price/discount" step starting Week 2.
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2))
    tax_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    delivery_fee: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    discount_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    promo_code: Mapped[str | None] = mapped_column(String, nullable=True)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2))

    # Why the order left the happy path, if it did (rejected/cancelled) —
    # feeds the "Order Cancellation Rate"/"Restaurant Acceptance Rate"
    # analytics metrics with an actual cause, not just a rate.
    status_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optimistic locking — concurrent updates from saga activities + customer
    # cancellation requests (docs/technical-documentation.md#data-consistency-across-services).
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", lazy="selectin")
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", lazy="selectin", order_by="OrderStatusHistory.created_at"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    menu_item_id: Mapped[str] = mapped_column(String)  # opaque ref to restaurant-service
    # Snapshotted at order time — restaurant-service's menu can change or the
    # item can be deleted later, but order history must stay accurate, and
    # cross-service joins aren't possible anyway (database-per-service).
    item_name_snapshot: Mapped[str] = mapped_column(String)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2))
    special_instructions: Mapped[str | None] = mapped_column(String, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    """Append-only audit trail of every order status transition — who moved
    it, from what, to what, and why. Written in the same transaction as the
    status change itself, so the trail can never disagree with `orders.status`.
    Week 1 only records the initial `created` row; Week 2's saga appends the
    rest."""

    __tablename__ = "order_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)  # None = order creation
    to_status: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)  # None = system/saga
    actor_role: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    order: Mapped["Order"] = relationship(back_populates="status_history")


class OutboxEvent(Base, OutboxEventMixin):
    __tablename__ = "outbox_events"
