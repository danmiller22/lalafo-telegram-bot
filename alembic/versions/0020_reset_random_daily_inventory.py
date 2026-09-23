"""Reset queued inventory for the random daytime schedule.

Revision ID: 0020_reset_random_daily_inventory
Revises: 0019_delete_disallowed_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_reset_random_daily_inventory"
down_revision = "0019_delete_disallowed_inventory"
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
