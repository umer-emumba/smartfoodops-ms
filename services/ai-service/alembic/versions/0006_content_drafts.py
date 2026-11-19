"""content_drafts: AI-written restaurant content, a draft until the owner
accepts it (docs/week5-plan.md step 5)

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
    op.create_table(
        "content_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("restaurant_id", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("applied", sa.JSON(), nullable=True),
        sa.Column("interaction_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_drafts_restaurant_created",
        "content_drafts",
        ["restaurant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_content_drafts_restaurant_created", table_name="content_drafts")
    op.drop_table("content_drafts")
