"""Discard checkout URLs that can no longer be used.

Revision ID: 0033_clear_terminal_finik_checkout_urls
Revises: 0032_payment_recovery_reminder
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_clear_terminal_finik_checkout_urls"
down_revision = "0032_payment_recovery_reminder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    payment_requests = sa.table(
        "payment_requests",
        sa.column("status", sa.String()),
        sa.column("provider_status", sa.String()),
        sa.column("provider_payment_url", sa.Text()),
    )
    op.execute(
        payment_requests.update()
        .where(
            sa.or_(
                payment_requests.c.status == "approved",
                payment_requests.c.provider_status.in_(
                    ("succeeded", "amount_mismatch", "failed")
                ),
            )
        )
        .values(provider_payment_url=None)
    )


def downgrade() -> None:
    # Checkout URLs are short-lived secrets and cannot be reconstructed safely.
    pass
