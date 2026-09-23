"""Retry inventory immediately with the broader fresh sources.

Revision ID: 0022_retry_broad_inventory
Revises: 0021_start_today_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_retry_broad_inventory"
down_revision = "0021_start_today_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM apartment_discovery_runs"))


def downgrade() -> None:
    pass
