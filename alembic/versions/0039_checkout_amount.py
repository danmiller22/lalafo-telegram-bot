"""Preserve the exact price of outstanding Finik checkouts."""
from alembic import op
import sqlalchemy as sa
revision = "0039_checkout_amount"
down_revision = "0038_wanted_finik"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("payment_requests", sa.Column("provider_expected_amount", sa.Integer(), nullable=True))
    op.execute("UPDATE payment_requests SET provider_expected_amount = 499 WHERE plan = 'week' AND provider_payment_id IS NOT NULL")

def downgrade():
    op.drop_column("payment_requests", "provider_expected_amount")
