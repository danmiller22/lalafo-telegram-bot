"""Persist rental feed market snapshots and monitor state."""
from alembic import op
import sqlalchemy as sa
revision = "0042_rental_feed_state"
down_revision = "0041_xpay_checkout_plan"
branch_labels = None
depends_on = None

def upgrade():
    sa.Table("rental_feed_state", sa.MetaData(), sa.Column("key", sa.String(64), primary_key=True), sa.Column("value", sa.JSON(), nullable=False)).create(op.get_bind(), checkfirst=True)

def downgrade():
    op.drop_table("rental_feed_state")
