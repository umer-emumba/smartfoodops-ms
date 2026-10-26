"""delivery assignment: rider location time, delivery offer/ETA/cancel
columns, delivery_offers, delivery_status_history

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DELIVERY_TIMESTAMPS = ("ready_at", "offer_expires_at", "dropoff_eta", "cancelled_at")


def upgrade() -> None:
    op.add_column(
        "riders", sa.Column("last_location_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("deliveries", sa.Column("customer_id", sa.String(), nullable=True))
    for column in _DELIVERY_TIMESTAMPS:
        op.add_column("deliveries", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_deliveries_rider_id_status", "deliveries", ["rider_id", "status"])

    op.create_table(
        "delivery_offers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("delivery_id", sa.String(), sa.ForeignKey("deliveries.id"), nullable=False),
        sa.Column("rider_id", sa.String(), sa.ForeignKey("riders.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("distance_km", sa.Float(), nullable=False),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delivery_offers_delivery_id", "delivery_offers", ["delivery_id"])
    op.create_index("ix_delivery_offers_rider_id_status", "delivery_offers", ["rider_id", "status"])

    op.create_table(
        "delivery_status_history",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("delivery_id", sa.String(), sa.ForeignKey("deliveries.id"), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("rider_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_delivery_status_history_delivery_id", "delivery_status_history", ["delivery_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_status_history_delivery_id", table_name="delivery_status_history")
    op.drop_table("delivery_status_history")
    op.drop_index("ix_delivery_offers_rider_id_status", table_name="delivery_offers")
    op.drop_index("ix_delivery_offers_delivery_id", table_name="delivery_offers")
    op.drop_table("delivery_offers")
    op.drop_index("ix_deliveries_rider_id_status", table_name="deliveries")
    for column in reversed(_DELIVERY_TIMESTAMPS):
        op.drop_column("deliveries", column)
    op.drop_column("deliveries", "customer_id")
    op.drop_column("riders", "last_location_at")
