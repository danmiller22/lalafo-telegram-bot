"""Persist XPAY transactions, including expired links awaiting reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "0040_xpay_attempts"
down_revision = "0039_checkout_amount"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("xpay_config", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False))
    op.create_table("xpay_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("payment_requests.id")),
        sa.Column("wanted_ad_id", sa.Integer(), sa.ForeignKey("wanted_ads.id")),
        sa.Column("provider_id", sa.String(128), unique=True),
        sa.Column("payment_url", sa.Text()),
        sa.Column("expected_amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_xpay_attempts_request_id", "xpay_attempts", ["request_id"])
    op.create_index("ix_xpay_attempts_wanted_ad_id", "xpay_attempts", ["wanted_ad_id"])

def downgrade():
    op.drop_table("xpay_attempts")
    op.drop_table("xpay_config")
