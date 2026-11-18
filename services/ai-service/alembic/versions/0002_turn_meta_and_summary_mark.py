"""conversation memory for the assistant graph: turns carry what was shown
(meta), conversations mark how far the summary reaches (docs/week5-plan.md
step 2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("summarized_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("conversation_turns", sa.Column("meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_turns", "meta")
    op.drop_column("conversations", "summarized_seq")
