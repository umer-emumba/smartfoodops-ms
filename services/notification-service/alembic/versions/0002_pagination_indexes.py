"""keyset pagination index for a user's notifications

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Replaces the single-column user_id index — the composite covers it.
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.create_index(
        "ix_notifications_user_id_created_at_id",
        "notifications",
        ["user_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_id_created_at_id", table_name="notifications")
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
