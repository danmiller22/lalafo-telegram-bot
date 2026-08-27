"""Track resumable Lalafo draft progress.

Revision ID: 0010_featured_draft
Revises: 0009_featured_review
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_featured_draft"
down_revision = "0009_featured_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_featured_publications",
        sa.Column("managed_lalafo_temp_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "daily_featured_publications",
        sa.Column(
            "managed_lalafo_uploaded_photos",
            sa.Integer(), nullable=False, server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "daily_featured_publications", "managed_lalafo_uploaded_photos"
    )
    op.drop_column("daily_featured_publications", "managed_lalafo_temp_id")
