"""keyset pagination indexes on orders, partial index on undispatched outbox rows

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # (customer_id, created_at, id) also serves plain customer_id lookups, so
    # the old single-column index is redundant write overhead.
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.create_index(
        "ix_orders_customer_id_created_at_id", "orders", ["customer_id", "created_at", "id"]
    )
    op.create_index("ix_orders_status_created_at_id", "orders", ["status", "created_at", "id"])
    op.create_index("ix_orders_created_at_id", "orders", ["created_at", "id"])
    op.create_index(
        "ix_outbox_events_undispatched",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_undispatched", table_name="outbox_events")
    op.drop_index("ix_orders_created_at_id", table_name="orders")
    op.drop_index("ix_orders_status_created_at_id", table_name="orders")
    op.drop_index("ix_orders_customer_id_created_at_id", table_name="orders")
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
