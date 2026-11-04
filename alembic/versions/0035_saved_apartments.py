"""Add customer apartment favorites.

Revision ID: 0035_saved_apartments
Revises: 0034_provider_admin_notification
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_saved_apartments"
down_revision = "0034_provider_admin_notification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_apartments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "apartment_id",
            sa.Integer(),
            sa.ForeignKey("apartments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "telegram_user_id", "apartment_id", name="uq_saved_user_apartment"
        ),
    )
    op.create_index(
        "ix_saved_user_created",
        "saved_apartments",
        ["telegram_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("saved_apartments")
