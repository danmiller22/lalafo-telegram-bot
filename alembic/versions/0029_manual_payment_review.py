"""Cancel outstanding automatic claim holds; retain requests for manual review."""
from alembic import op
import sqlalchemy as sa

revision = "0029_manual_payment_review"
down_revision = "0028_payment_claim_hold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE payment_requests SET payment_claimed_at = NULL WHERE status = 'pending'"))


def downgrade() -> None:
    # A cancelled timer must never be reinstated without a new customer action.
    pass
