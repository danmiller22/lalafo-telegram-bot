"""Track one-time featured advertisement notifications.

Revision ID: 0011_featured_events
Revises: 0010_featured_draft
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_featured_events"
down_revision = "0010_featured_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "new_ad_notified_at",
        "expiring_notified_at",
        "deactivated_notified_at",
    ):
        op.add_column(
            "daily_featured_publications",
            sa.Column(name, sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for name in (
        "deactivated_notified_at",
        "expiring_notified_at",
        "new_ad_notified_at",
    ):
        op.drop_column("daily_featured_publications", name)
