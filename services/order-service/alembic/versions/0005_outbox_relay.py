"""outbox relay bookkeeping: trace context, publish attempts, last error

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("trace_context", sa.String(), nullable=True))
    op.add_column(
        "outbox_events",
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("outbox_events", sa.Column("last_error", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "publish_attempts")
    op.drop_column("outbox_events", "trace_context")
