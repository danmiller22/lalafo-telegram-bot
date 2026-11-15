"""Record individual Finik payments for apartment-search requests."""
from alembic import op
import sqlalchemy as sa
revision = "0038_wanted_finik"
down_revision = "0037_personal_match_reservations"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("wanted_payments", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wanted_ad_id", sa.Integer(), sa.ForeignKey("wanted_ads.id"), nullable=False),
        sa.Column("provider_payment_id", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("payment_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("paid_at", sa.DateTime(timezone=True)))
    op.create_index("ix_wanted_payments_wanted_ad_id", "wanted_payments", ["wanted_ad_id"])

def downgrade():
    op.drop_table("wanted_payments")
