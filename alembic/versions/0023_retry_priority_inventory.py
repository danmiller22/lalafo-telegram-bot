"""Retry discovery immediately with fresh direct apartment sources.

Revision ID: 0023_retry_priority_inventory
Revises: 0022_retry_broad_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_retry_priority_inventory"
down_revision = "0022_retry_broad_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM apartment_discovery_runs"))


def downgrade() -> None:
    pass
