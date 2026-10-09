from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrderStatus(StrEnum):
    """The one definition of order statuses — used for the column default,
    every transition, and API filters, so they can't drift apart.

    Allowed transitions between them: app/state_machine.py.
    """

    CREATED = "created"
    AWAITING_RESTAURANT_ACCEPTANCE = "awaiting_restaurant_acceptance"
    CONFIRMED = "confirmed"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Order(Base):
    __tablename__ = "orders"
    # Scoped per customer, not global — the key is client-generated, so two
    # customers' clients may independently pick the same value.
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_orders_customer_idempotency_key"
        ),
        # Keyset pagination (sfo_common.pagination: created_at DESC, id DESC)
        # for a customer's own orders, the admin status filter, and the
        # unfiltered admin list.
        Index("ix_orders_customer_id_created_at_id", "customer_id", "created_at", "id"),
        Index("ix_orders_status_created_at_id", "status", "created_at", "id"),
        Index("ix_orders_created_at_id", "created_at", "id"),
        # A restaurant's order queue (GET /v1/orders/restaurant/{id}), with
        # and without the status filter.
        Index(
            "ix_orders_restaurant_id_status_created_at_id",
            "restaurant_id",
            "status",
            "created_at",
            "id",
        ),
        Index("ix_orders_restaurant_id_created_at_id", "restaurant_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    # Human-facing, sequential — distinct from the UUID primary key, for
    # customer support / receipts (docs/technical-documentation.md).
    order_number: Mapped[int] = mapped_column(Integer, Identity(start=1000), unique=True)
    customer_id: Mapped[str] = mapped_column(String)  # opaque ref to user-service
    restaurant_id: Mapped[str] = mapped_column(String)  # opaque ref to restaurant-service
    # Snapshotted from restaurant-service at order time (week2-plan D10): the
    # owner authorizes accept/reject here without a cross-service call, and
    # the restaurant's location is the delivery pickup point. Nullable only
    # for orders created before these columns existed.
    restaurant_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    restaurant_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    restaurant_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default=OrderStatus.CREATED)

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
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    promo_code: Mapped[str | None] = mapped_column(String, nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    payment_method: Mapped[str] = mapped_column(String, default="card_simulated")
    # Post-delivery refunds (week2-plan D13). The order stays `delivered`; a
    # refund is a fact about it, not a lifecycle state.
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Why the order left the happy path, if it did (rejected/cancelled) —
    # feeds the "Order Cancellation Rate"/"Restaurant Acceptance Rate"
    # analytics metrics with an actual cause, not just a rate.
    status_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    special_instructions: Mapped[str | None] = mapped_column(String, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    """Append-only audit trail of every order status transition — who moved
    it, from what, to what, and why. Written in the same transaction as the
    status change itself, so the trail can never disagree with `orders.status`.
    The `created` row is written by POST /v1/orders; every later one by the
    saga's transition_order activity (app/activities.py). A post-delivery
    refund appends a delivered → delivered row."""

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
    # Week 3's relay polls undispatched rows oldest-first; a partial index
    # stays small because dispatched rows drop out of it.
    __table_args__ = (
        Index(
            "ix_outbox_events_undispatched",
            "created_at",
            postgresql_where=text("dispatched_at IS NULL"),
        ),
    )
