"""rider_verification_history audit trail

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
    op.create_table(
        "rider_verification_history",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("rider_id", sa.String(), sa.ForeignKey("riders.id"), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("actor_role", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_rider_verification_history_rider_id", "rider_verification_history", ["rider_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_rider_verification_history_rider_id", table_name="rider_verification_history")
    op.drop_table("rider_verification_history")
