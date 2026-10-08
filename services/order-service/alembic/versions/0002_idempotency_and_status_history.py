"""orders idempotency key + order_status_history audit trail

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("idempotency_key", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("request_hash", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_orders_customer_idempotency_key", "orders", ["customer_id", "idempotency_key"]
    )

    op.create_table(
        "order_status_history",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("actor_role", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_status_history_order_id", "order_status_history", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_order_status_history_order_id", table_name="order_status_history")
    op.drop_table("order_status_history")
    op.drop_constraint("uq_orders_customer_idempotency_key", "orders", type_="unique")
    op.drop_column("orders", "request_hash")
    op.drop_column("orders", "idempotency_key")
