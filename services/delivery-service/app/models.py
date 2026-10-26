from datetime import UTC, datetime

from sfo_common import new_id
from sfo_events import OutboxEventMixin
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# rider status: available | busy | offline. `busy` is also how a rider is
# held while an offer is open or a delivery is in progress (app/activities.py).
RIDER_STATUS_OFFLINE = "offline"

# delivery status: app/state_machine.py.


class Rider(Base):
    __tablename__ = "riders"
    # Keyset pagination of the admin rider list (sfo_common.pagination),
    # unfiltered and by verification_status.
    __table_args__ = (
        Index("ix_riders_created_at_id", "created_at", "id"),
        Index(
            "ix_riders_verification_status_created_at_id",
            "verification_status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String, unique=True, index=True
    )  # opaque ref to user-service
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
    # Written at most every 30s (week2-plan D6) — live positions are in
    # Redis. Also what the stale-rider detector checks (app/stale_riders.py).
    last_location_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    # A rider's current delivery (location pings → live tracking), and the
    # stale-rider detector's scan of assigned deliveries.
    __table_args__ = (Index("ix_deliveries_rider_id_status", "rider_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(
        String, unique=True, index=True
    )  # opaque ref to order-service
    # Snapshotted from the order — who may read this delivery. Nullable only
    # for rows created before it existed.
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The rider currently holding the delivery (offered or assigned); None
    # while waiting for one.
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

    # When the food should be ready; the pickup ETA is never earlier.
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offer_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pickup_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dropoff_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class DeliveryOffer(Base):
    """Every offer of a delivery to a rider and how it ended — the audit
    trail behind reassignments, and rider-utilization analytics. At most one
    `pending` offer per delivery."""

    __tablename__ = "delivery_offers"
    __table_args__ = (
        # A rider's open offer (GET /v1/riders/me/offer).
        Index("ix_delivery_offers_rider_id_status", "rider_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("deliveries.id"), index=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"))
    # pending | accepted | declined | expired | dropped | cancelled | completed
    status: Mapped[str] = mapped_column(String, default="pending")
    distance_km: Mapped[float] = mapped_column(Float)  # rider → pickup when offered
    offered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryStatusHistory(Base):
    """Append-only audit trail of delivery status changes, written in the
    same transaction as the change (same shape as the other services')."""

    __tablename__ = "delivery_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("deliveries.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)  # None = created
    to_status: Mapped[str] = mapped_column(String)
    rider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OutboxEvent(Base, OutboxEventMixin):
    __tablename__ = "outbox_events"
