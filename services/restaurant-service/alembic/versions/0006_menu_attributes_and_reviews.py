"""richer catalog for search (dietary tags, spice level, calories, restaurant
description) and customer reviews with rating aggregates (week4-plan step 1)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("description", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "restaurants",
        sa.Column("rating_sum", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "restaurants",
        sa.Column("review_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "menu_items",
        sa.Column("dietary_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "menu_items",
        sa.Column("spice_level", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("menu_items", sa.Column("calories", sa.Integer(), nullable=True))
    op.create_table(
        "reviews",
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("restaurant_id", sa.String(), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating"),
    )
    op.create_index(
        "ix_reviews_restaurant_id_created_at_order_id",
        "reviews",
        ["restaurant_id", "created_at", "order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_reviews_restaurant_id_created_at_order_id", table_name="reviews")
    op.drop_table("reviews")
    op.drop_column("menu_items", "calories")
    op.drop_column("menu_items", "spice_level")
    op.drop_column("menu_items", "dietary_tags")
    op.drop_column("restaurants", "review_count")
    op.drop_column("restaurants", "rating_sum")
    op.drop_column("restaurants", "description")
