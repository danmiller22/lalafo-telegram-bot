"""Rebuild the current queue so the daytime schedule starts today.

Revision ID: 0021_start_today_inventory
Revises: 0020_random_daily_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_start_today_inventory"
down_revision = "0020_random_daily_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM apartment_inventory_queue "
            "WHERE status IN ('queued', 'publishing')"
        )
    )
    connection.execute(sa.text("DELETE FROM apartment_discovery_runs"))


def downgrade() -> None:
    # Removed reservations are deliberately not recreated.
    pass
