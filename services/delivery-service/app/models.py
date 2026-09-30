from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# rider status: available | busy | offline
RIDER_STATUS_OFFLINE = "offline"

# delivery status: assigned -> picked_up -> delivered (full logic in Week 2)


class Rider(Base):
    __tablename__ = "riders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # opaque ref to user-service
    status: Mapped[str] = mapped_column(String, default=RIDER_STATUS_OFFLINE)
    # Separate from `status` (self-reported availability) — this is the
    # system_admin vetting gate: pending_approval | approved | suspended. Week
    # 2's assignment query must additionally require verification_status ==
    # "approved", not just status == "available".
    verification_status: Mapped[str] = mapped_column(String, default="pending_approval")
    # Onboarding fields.
    vehicle_type: Mapped[str] = mapped_column(String, default="bike")  # bike | scooter | car
    license_plate: Mapped[str | None] = mapped_column(String, nullable=True)
    # Real numeric columns, not a "lat,lng" string blob — required for
    # spatially-indexable proximity queries (docs/technical-documentation.md).
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # opaque ref to order-service
    rider_id: Mapped[str | None] = mapped_column(ForeignKey("riders.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending_assignment")

    # Snapshotted at assignment time (from restaurant-service / order-service)
    # — avoids a repeated cross-service call on every tracking read, and
    # keeps the historical delivery record accurate regardless of later
    # changes upstream.
    pickup_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    pickup_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    dropoff_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    dropoff_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    pickup_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How many times reassigned after a rider timeout/failure (Week 2) — feeds
    # delivery-reliability analytics.
    reassignment_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Optimistic locking — prevents double-assignment races (docs/architecture.md).
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}


class RiderVerificationHistory(Base):
    """Append-only audit trail of rider vetting decisions (registration,
    approval, suspension) — who, when, why. Written in the same transaction
    as the verification_status change."""

    __tablename__ = "rider_verification_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)  # None = registration
    to_status: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    actor_role: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OutboxEvent(Base, OutboxEventMixin):
    __tablename__ = "outbox_events"
