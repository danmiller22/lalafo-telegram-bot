"""Add durable admin receipts for automatic Finik payments.

Revision ID: 0034_provider_admin_notification
Revises: 0033_clear_finik_urls
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_provider_admin_notification"
down_revision = "0033_clear_finik_urls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_requests",
        sa.Column("provider_admin_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "payment_requests",
        sa.Column("provider_admin_notification_token", sa.String(64), nullable=True),
    )
    op.add_column(
        "payment_requests",
        sa.Column(
            "provider_admin_notification_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("payment_requests", "provider_admin_notification_claimed_at")
    op.drop_column("payment_requests", "provider_admin_notification_token")
    op.drop_column("payment_requests", "provider_admin_message_id")
