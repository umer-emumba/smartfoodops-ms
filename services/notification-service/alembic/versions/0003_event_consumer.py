"""event consumer: processed_events; notifications get recipient role, source
event (unique per event/user/channel), last attempt time; in-app inbox and
pending-sweep partial indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("consumer", sa.String(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Nothing wrote notifications before this revision, so the table is
    # empty and the new NOT NULL columns need no backfill.
    op.add_column("notifications", sa.Column("recipient_role", sa.String(), nullable=False))
    op.add_column("notifications", sa.Column("event_id", sa.String(), nullable=False))
    op.add_column(
        "notifications", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_notifications_event_user_channel", "notifications", ["event_id", "user_id", "channel"]
    )
    # The inbox lists in-app notifications only; the partial index replaces
    # the all-channel one.
    op.drop_index("ix_notifications_user_id_created_at_id", table_name="notifications")
    op.create_index(
        "ix_notifications_inbox",
        "notifications",
        ["user_id", "created_at", "id"],
        postgresql_where=sa.text("channel = 'in_app'"),
    )
    op.create_index(
        "ix_notifications_pending",
        "notifications",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_pending", table_name="notifications")
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.create_index(
        "ix_notifications_user_id_created_at_id",
        "notifications",
        ["user_id", "created_at", "id"],
    )
    op.drop_constraint("uq_notifications_event_user_channel", "notifications")
    op.drop_column("notifications", "last_attempt_at")
    op.drop_column("notifications", "event_id")
    op.drop_column("notifications", "recipient_role")
    op.drop_table("processed_events")
