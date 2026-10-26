"""order lifecycle: restaurant snapshot, payment method, refunds, lifecycle
timestamps, restaurant order-queue indexes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMPS = ("refunded_at", "assigned_at", "picked_up_at", "delivered_at")


def upgrade() -> None:
    op.add_column("orders", sa.Column("restaurant_admin_id", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("restaurant_lat", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("restaurant_lng", sa.Float(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("payment_method", sa.String(), nullable=False, server_default="card_simulated"),
    )
    op.add_column(
        "orders",
        sa.Column(
            "refunded_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")
        ),
    )
    for column in _TIMESTAMPS:
        op.add_column("orders", sa.Column(column, sa.DateTime(timezone=True), nullable=True))

    # (restaurant_id, created_at, id) also serves plain restaurant_id lookups,
    # so the single-column index is redundant (same as 0003 for customer_id).
    op.drop_index("ix_orders_restaurant_id", table_name="orders")
    op.create_index(
        "ix_orders_restaurant_id_status_created_at_id",
        "orders",
        ["restaurant_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_orders_restaurant_id_created_at_id", "orders", ["restaurant_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_orders_restaurant_id_created_at_id", table_name="orders")
    op.drop_index("ix_orders_restaurant_id_status_created_at_id", table_name="orders")
    op.create_index("ix_orders_restaurant_id", "orders", ["restaurant_id"])
    for column in reversed(_TIMESTAMPS):
        op.drop_column("orders", column)
    op.drop_column("orders", "refunded_amount")
    op.drop_column("orders", "payment_method")
    op.drop_column("orders", "restaurant_lng")
    op.drop_column("orders", "restaurant_lat")
    op.drop_column("orders", "restaurant_admin_id")
