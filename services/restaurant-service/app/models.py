from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Restaurant(Base):
    __tablename__ = "restaurants"
    # Keyset pagination (sfo_common.pagination: created_at DESC, id DESC) for
    # the public list (always status = active, optionally by city) and the
    # admin list's status filter.
    __table_args__ = (
        Index("ix_restaurants_status_city_created_at_id", "status", "city", "created_at", "id"),
        Index("ix_restaurants_status_created_at_id", "status", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    admin_user_id: Mapped[str] = mapped_column(String, index=True)  # opaque ref to user-service
    name: Mapped[str] = mapped_column(String)
    address: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String, index=True)
    # Real coordinates, not just a text address — required for proximity-based
    # rider assignment and ETA calculation (docs/architecture.md).
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    contact_phone: Mapped[str] = mapped_column(String)
    cuisine_type: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # pending_approval (initial, set by the system) | active (system_admin-
    # approved) | suspended (system_admin-suspended, from either state) — see
    # docs/architecture.md#restaurant--rider-approval.
    status: Mapped[str] = mapped_column(String, default="pending_approval")
    # "Restaurant capacity must be checked" (PRD) — simple manual toggle +
    # concurrent-order cap, not a full weekly-hours schedule (documented
    # simplification, see docs/technical-documentation.md).
    is_accepting_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_orders: Mapped[int] = mapped_column(Integer, default=20)
    avg_prep_time_minutes: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class MenuCategory(Base):
    __tablename__ = "menu_categories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    name: Mapped[str] = mapped_column(String)


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("menu_categories.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, default="")
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    stock_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Optimistic locking — concurrent stock decrements during the order saga
    # (docs/technical-documentation.md#data-consistency-across-services).
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}


class Promotion(Base):
    """Restaurant-issued discount codes — backs the PRD's "pricing and
    discounts must be applied" order-flow requirement. Redemption/validation
    logic is applied by the order saga starting Week 2."""

    __tablename__ = "promotions"
    # A code is only unique within its own restaurant, not globally — two
    # different restaurants must each be able to run their own "WELCOME10".
    # Declared exactly as migration 0001 creates it (a unique index, not a
    # named constraint), so autogenerate doesn't report drift.
    __table_args__ = (
        Index("ux_promotions_restaurant_id_code", "restaurant_id", "code", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    code: Mapped[str] = mapped_column(String, index=True)
    discount_type: Mapped[str] = mapped_column(String)  # percentage | fixed_amount
    discount_value: Mapped[float] = mapped_column(Numeric(10, 2))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RestaurantStatusHistory(Base):
    """Append-only audit trail of restaurant status changes (onboarding,
    approval, suspension) — who did it, when, and why. Written in the same
    transaction as the status change itself."""

    __tablename__ = "restaurant_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)  # None = onboarding
    to_status: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    actor_role: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OutboxEvent(Base, OutboxEventMixin):
    __tablename__ = "outbox_events"
