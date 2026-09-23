"""Release the cancelled discovery lease and start inventory immediately.

Revision ID: 0024_release_stale_discovery
Revises: 0023_retry_priority_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0024_release_stale_discovery"
down_revision = "0023_retry_priority_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM apartment_discovery_runs"))


def downgrade() -> None:
    pass
