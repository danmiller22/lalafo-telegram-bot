"""Delete queued inventory outside the studio and one-room policy.

Revision ID: 0019_delete_disallowed_inventory
Revises: 0018_inventory_query_indexes
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_delete_disallowed_inventory"
down_revision = "0018_inventory_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "DELETE FROM apartment_inventory_queue "
            "WHERE apartment_id IN ("
            "SELECT id FROM apartments WHERE rooms NOT IN ('studio', '1')"
            ")"
        )
    )


def downgrade() -> None:
    # Deleted queue reservations cannot and should not be recreated.
    pass
