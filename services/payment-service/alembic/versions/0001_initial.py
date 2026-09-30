"""initial schema: payments, outbox_events

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
        "payments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payment_method", sa.String(), nullable=False, server_default="card_simulated"),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("refunded_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=True)
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])
    op.create_index("ix_payments_idempotency_key", "payments", ["idempotency_key"], unique=True)

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
    op.drop_index("ix_payments_idempotency_key", table_name="payments")
    op.drop_index("ix_payments_customer_id", table_name="payments")
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")
