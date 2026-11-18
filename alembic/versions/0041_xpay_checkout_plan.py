"""Preserve the purchased tariff independently of mutable card requests."""
from alembic import op
import sqlalchemy as sa
revision = "0041_xpay_checkout_plan"
down_revision = "0040_xpay_attempts"
branch_labels = None
depends_on = None
def upgrade():
    op.add_column("apartment_search_profiles", sa.Column("include_current_offers", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("xpay_attempts", sa.Column("checkout_plan", sa.String(24), nullable=True))
    op.execute("UPDATE xpay_attempts SET checkout_plan = 'week' WHERE request_id IS NOT NULL")
def downgrade():
    op.drop_column("apartment_search_profiles", "include_current_offers")
    op.drop_column("xpay_attempts", "checkout_plan")
