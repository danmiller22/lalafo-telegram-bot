"""Allow the payment ledger to record access without an expiry."""

from alembic import op
import sqlalchemy as sa

revision = "0026_lifetime_payment_access"
down_revision = "0025_availability_and_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("payment_history", "access_expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade() -> None:
    # Keep lifetime records when restoring the old non-null schema.
    op.execute("UPDATE payment_history SET access_expires_at = '9999-12-31 00:00:00+00' WHERE access_expires_at IS NULL")
    op.alter_column("payment_history", "access_expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
