"""Add a durable, one-time checkout recovery reminder.

Revision ID: 0032_payment_recovery_reminder
Revises: 0031_payment_notification_lease
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_payment_recovery_reminder"
down_revision = "0031_payment_notification_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_requests",
        sa.Column("payment_reminder_sent_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "payment_requests",
        sa.Column("payment_reminder_claimed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_payment_reminder_due",
        "payment_requests",
        ["status", "payment_reminder_sent_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_payment_reminder_due", table_name="payment_requests")
    op.drop_column("payment_requests", "payment_reminder_claimed_at")
    op.drop_column("payment_requests", "payment_reminder_sent_at")
