"""Persist the one-minute customer claim hold across restarts."""
from alembic import op
import sqlalchemy as sa

revision = "0028_payment_claim_hold"
down_revision = "0027_personal_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_requests", sa.Column("payment_claimed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_payment_claim_due", "payment_requests", ["status", "payment_claimed_at"])


def downgrade() -> None:
    op.drop_index("ix_payment_claim_due", table_name="payment_requests")
    op.drop_column("payment_requests", "payment_claimed_at")
