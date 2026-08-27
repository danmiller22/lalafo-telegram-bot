"""Make weekly access the default payment plan."""

from alembic import op
import sqlalchemy as sa


revision = "0007_weekly_only"
down_revision = "0006_payment_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payment_requests") as batch_op:
        batch_op.alter_column(
            "plan",
            existing_type=sa.String(length=16),
            server_default="week",
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("payment_requests") as batch_op:
        batch_op.alter_column(
            "plan",
            existing_type=sa.String(length=16),
            server_default="single",
            existing_nullable=False,
        )
