"""Add paid apartment wanted ads."""

from alembic import op
import sqlalchemy as sa

revision = "0002_wanted_ads"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wanted_ads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("rooms", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=255), nullable=False),
        sa.Column("budget", sa.Integer(), nullable=False),
        sa.Column("move_in", sa.String(length=100), nullable=False),
        sa.Column("tenants", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("contact", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("admin_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wanted_ads_telegram_user_id", "wanted_ads", ["telegram_user_id"])
    op.create_index(
        "ix_wanted_ads_status_created", "wanted_ads", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_wanted_ads_status_created", table_name="wanted_ads")
    op.drop_index("ix_wanted_ads_telegram_user_id", table_name="wanted_ads")
    op.drop_table("wanted_ads")
