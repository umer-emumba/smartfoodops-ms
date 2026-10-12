"""payment lifecycle: capture/void timestamps, refunds table

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payment_id", sa.String(), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])


def downgrade() -> None:
    op.drop_index("ix_refunds_payment_id", table_name="refunds")
    op.drop_table("refunds")
    op.drop_column("payments", "voided_at")
    op.drop_column("payments", "captured_at")
