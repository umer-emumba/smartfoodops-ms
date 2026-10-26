"""order saga: restaurant capacity counter, stock reservations, promotion
redemptions

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


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("active_order_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "stock_reservations",
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("restaurant_id", sa.String(), sa.ForeignKey("restaurants.id"), nullable=True),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_stock_reservations_restaurant_id", "stock_reservations", ["restaurant_id"])
    op.create_table(
        "promotion_redemptions",
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("promotion_id", sa.String(), sa.ForeignKey("promotions.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_promotion_redemptions_promotion_id", "promotion_redemptions", ["promotion_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_promotion_redemptions_promotion_id", table_name="promotion_redemptions")
    op.drop_table("promotion_redemptions")
    op.drop_index("ix_stock_reservations_restaurant_id", table_name="stock_reservations")
    op.drop_table("stock_reservations")
    op.drop_column("restaurants", "active_order_count")
