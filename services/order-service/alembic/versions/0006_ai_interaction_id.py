"""orders.ai_interaction_id: the AI assistant answer an order came from
(docs/week5-plan.md step 6)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("ai_interaction_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "ai_interaction_id")
