"""Recover administrator notifications interrupted by worker restarts."""
from alembic import op
import sqlalchemy as sa

revision = "0031_payment_notification_lease"
down_revision = "0030_requested_card_promotions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_requests", sa.Column("admin_notification_token", sa.String(64)))
    op.add_column("payment_requests", sa.Column("admin_notification_claimed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("payment_requests", "admin_notification_claimed_at")
    op.drop_column("payment_requests", "admin_notification_token")
