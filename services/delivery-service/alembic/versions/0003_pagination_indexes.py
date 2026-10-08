"""keyset pagination indexes for the admin rider list

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_riders_created_at_id", "riders", ["created_at", "id"])
    op.create_index(
        "ix_riders_verification_status_created_at_id",
        "riders",
        ["verification_status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_riders_verification_status_created_at_id", table_name="riders")
    op.drop_index("ix_riders_created_at_id", table_name="riders")
