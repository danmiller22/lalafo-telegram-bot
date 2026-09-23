"""Add indexes for the autonomous inventory hot path.

Revision ID: 0018_inventory_query_indexes
Revises: 0017_finik_auto_payments
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_inventory_query_indexes"
down_revision = "0017_finik_auto_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("apartments")}
    if "ix_apartment_inventory_fresh" not in indexes:
        op.create_index(
            "ix_apartment_inventory_fresh",
            "apartments",
            ["active", "publication_status", "rooms", "last_seen_at"],
        )
    if "ix_apartment_published_day" not in indexes:
        op.create_index(
            "ix_apartment_published_day",
            "apartments",
            ["publication_status", "published_at", "seller_type"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("apartments")}
    if "ix_apartment_published_day" in indexes:
        op.drop_index("ix_apartment_published_day", table_name="apartments")
    if "ix_apartment_inventory_fresh" in indexes:
        op.drop_index("ix_apartment_inventory_fresh", table_name="apartments")
