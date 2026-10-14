"""Encrypted payment configuration, separate from listing and responder state."""
from alembic import op
import sqlalchemy as sa

revision = "0032_payment_runtime_config"
down_revision = "0031_payment_notification_lease"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("payment_runtime_config", sa.Column("key", sa.String(64), primary_key=True), sa.Column("value", sa.Text(), nullable=False))


def downgrade():
    op.drop_table("payment_runtime_config")
