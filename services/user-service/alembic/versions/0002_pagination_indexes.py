"""keyset pagination index for the admin user list

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
    # Matches sfo_common.pagination's ORDER BY created_at DESC, id DESC
    # (scanned backwards), so GET /v1/users pages without a sort.
    op.create_index("ix_users_created_at_id", "users", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_users_created_at_id", table_name="users")
