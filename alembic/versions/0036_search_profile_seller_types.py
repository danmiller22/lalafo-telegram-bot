"""Store selected apartment author types in matching profiles.

Revision ID: 0036_search_profile_seller_types
Revises: 0035_saved_apartments
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_search_profile_seller_types"
down_revision = "0035_saved_apartments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "apartment_search_profiles",
        sa.Column("seller_types", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("apartment_search_profiles", "seller_types")
