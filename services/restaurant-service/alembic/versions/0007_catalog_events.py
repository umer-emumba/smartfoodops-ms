"""restaurant-events: a per-restaurant catalog version for event ordering,
and the outbox index the relay scans (week4-plan step 2)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("catalog_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    # What the relay's "oldest undispatched rows" query scans (order- and
    # delivery-service have the same index).
    op.create_index(
        "ix_outbox_events_undispatched",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_undispatched", table_name="outbox_events")
    op.drop_column("restaurants", "catalog_version")
