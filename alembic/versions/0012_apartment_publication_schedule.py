"""Add a shared two-hour publication clock and renewable lease.

Revision ID: 0012_apartment_schedule
Revises: 0011_featured_events
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_apartment_schedule"
down_revision = "0011_featured_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("apartment_publication_schedule"):
        return
    op.create_table(
        "apartment_publication_schedule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("lease_token", sa.String(64), unique=True),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_published_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("apartment_publication_schedule"):
        op.drop_table("apartment_publication_schedule")
