"""keyset pagination indexes for the public and admin restaurant lists

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
    op.create_index(
        "ix_restaurants_status_city_created_at_id",
        "restaurants",
        ["status", "city", "created_at", "id"],
    )
    op.create_index(
        "ix_restaurants_status_created_at_id", "restaurants", ["status", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_restaurants_status_created_at_id", table_name="restaurants")
    op.drop_index("ix_restaurants_status_city_created_at_id", table_name="restaurants")
