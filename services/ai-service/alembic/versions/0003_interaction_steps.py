"""ai_interactions.steps: tokens and time per assistant step, to see where
a turn's tokens go (docs/week5-plan.md step 2)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_interactions", sa.Column("steps", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_interactions", "steps")
