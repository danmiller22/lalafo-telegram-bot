"""Track source availability and payment terms consent.

Revision ID: 0025_availability_and_terms
Revises: 0024_release_stale_discovery
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_availability_and_terms"
down_revision = "0024_release_stale_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "apartments",
        sa.Column("availability_status", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.add_column("apartments", sa.Column("source_description", sa.Text()))
    op.add_column("apartments", sa.Column("availability_checked_at", sa.DateTime(timezone=True)))
    op.add_column("apartments", sa.Column("availability_reason", sa.String(length=64)))
    op.create_table(
        "terms_consents",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        sa.text(
            "DELETE FROM apartment_inventory_queue "
            "WHERE status = 'skipped' AND last_error = 'not_confirmed_owner' "
            "AND apartment_id IN (SELECT id FROM apartments WHERE "
            "source_url LIKE 'https://lalafo.kg/%' OR "
            "source_url LIKE 'https://www.lalafo.kg/%' OR "
            "source_url LIKE 'https://t.me/%' OR "
            "source_url LIKE 'manual://telegram/%')"
        )
    )


def downgrade() -> None:
    op.drop_table("terms_consents")
    op.drop_column("apartments", "availability_reason")
    op.drop_column("apartments", "availability_checked_at")
    op.drop_column("apartments", "availability_status")
    op.drop_column("apartments", "source_description")
