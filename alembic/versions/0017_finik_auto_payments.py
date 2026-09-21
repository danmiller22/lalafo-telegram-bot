"""Add Finik Web SDK payment tracking.

Revision ID: 0017_finik_auto_payments
Revises: 0016_apartment_seller_type
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_finik_auto_payments"
down_revision = "0016_apartment_seller_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("payment_requests")}
    if "provider_payment_id" not in columns:
        op.add_column("payment_requests", sa.Column("provider_payment_id", sa.String(64)))
    if "provider_payment_url" not in columns:
        op.add_column("payment_requests", sa.Column("provider_payment_url", sa.Text()))
    if "provider_status" not in columns:
        op.add_column("payment_requests", sa.Column("provider_status", sa.String(32)))
    indexes = {item["name"] for item in inspector.get_indexes("payment_requests")}
    if "ix_payment_provider_id" not in indexes:
        op.create_index(
            "ix_payment_provider_id",
            "payment_requests",
            ["provider_payment_id"],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("payment_requests")}
    if "ix_payment_provider_id" in indexes:
        op.drop_index("ix_payment_provider_id", table_name="payment_requests")
    columns = {item["name"] for item in inspector.get_columns("payment_requests")}
    for name in ("provider_status", "provider_payment_url", "provider_payment_id"):
        if name in columns:
            op.drop_column("payment_requests", name)
