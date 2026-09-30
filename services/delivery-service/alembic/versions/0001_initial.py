"""initial schema: riders, deliveries, outbox_events

Revision ID: 0001
Revises:
Create Date: 2026-09-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "riders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="offline"),
        sa.Column("verification_status", sa.String(), nullable=False, server_default="pending_approval"),
        sa.Column("vehicle_type", sa.String(), nullable=False, server_default="bike"),
        sa.Column("license_plate", sa.String(), nullable=True),
        sa.Column("last_lat", sa.Float(), nullable=True),
        sa.Column("last_lng", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_riders_user_id", "riders", ["user_id"], unique=True)

    op.create_table(
        "deliveries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("rider_id", sa.String(), sa.ForeignKey("riders.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_assignment"),
        sa.Column("pickup_lat", sa.Float(), nullable=True),
        sa.Column("pickup_lng", sa.Float(), nullable=True),
        sa.Column("dropoff_lat", sa.Float(), nullable=True),
        sa.Column("dropoff_lng", sa.Float(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("pickup_eta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reassignment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_deliveries_order_id", "deliveries", ["order_id"], unique=True)

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_index("ix_deliveries_order_id", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_riders_user_id", table_name="riders")
    op.drop_table("riders")
