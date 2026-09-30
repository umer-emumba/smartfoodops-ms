"""initial schema: restaurants, menu_categories, menu_items, promotions, outbox_events

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
        "restaurants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("admin_user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("contact_phone", sa.String(), nullable=False),
        sa.Column("cuisine_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_approval"),
        sa.Column("is_accepting_orders", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_concurrent_orders", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("avg_prep_time_minutes", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_restaurants_admin_user_id", "restaurants", ["admin_user_id"])
    op.create_index("ix_restaurants_city", "restaurants", ["city"])
    op.create_index("ix_restaurants_cuisine_type", "restaurants", ["cuisine_type"])

    op.create_table(
        "menu_categories",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("restaurant_id", sa.String(), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_index("ix_menu_categories_restaurant_id", "menu_categories", ["restaurant_id"])

    op.create_table(
        "menu_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("restaurant_id", sa.String(), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("category_id", sa.String(), sa.ForeignKey("menu_categories.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stock_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_menu_items_restaurant_id", "menu_items", ["restaurant_id"])
    op.create_index("ix_menu_items_category_id", "menu_items", ["category_id"])

    op.create_table(
        "promotions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("restaurant_id", sa.String(), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("discount_type", sa.String(), nullable=False),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_promotions_restaurant_id", "promotions", ["restaurant_id"])
    op.create_index("ix_promotions_code", "promotions", ["code"])
    op.create_index(
        "ux_promotions_restaurant_id_code", "promotions", ["restaurant_id", "code"], unique=True
    )

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
    op.drop_index("ux_promotions_restaurant_id_code", table_name="promotions")
    op.drop_index("ix_promotions_code", table_name="promotions")
    op.drop_index("ix_promotions_restaurant_id", table_name="promotions")
    op.drop_table("promotions")
    op.drop_index("ix_menu_items_category_id", table_name="menu_items")
    op.drop_index("ix_menu_items_restaurant_id", table_name="menu_items")
    op.drop_table("menu_items")
    op.drop_index("ix_menu_categories_restaurant_id", table_name="menu_categories")
    op.drop_table("menu_categories")
    op.drop_index("ix_restaurants_cuisine_type", table_name="restaurants")
    op.drop_index("ix_restaurants_city", table_name="restaurants")
    op.drop_index("ix_restaurants_admin_user_id", table_name="restaurants")
    op.drop_table("restaurants")
