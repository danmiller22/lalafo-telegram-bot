"""Add paid plans, receipt storage and expiring weekly access."""

from alembic import op
import sqlalchemy as sa


revision = "0006_payment_plans"
down_revision = "0005_subletting_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("payment_requests")}
    additions = (
        ("plan", sa.Column("plan", sa.String(16), server_default="single", nullable=False)),
        ("receipt_file_id", sa.Column("receipt_file_id", sa.String(255), nullable=True)),
        ("receipt_file_type", sa.Column("receipt_file_type", sa.String(16), nullable=True)),
        (
            "access_expires_at",
            sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("payment_requests", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("payment_requests")}
    for name in ("access_expires_at", "receipt_file_type", "receipt_file_id", "plan"):
        if name in columns:
            op.drop_column("payment_requests", name)
