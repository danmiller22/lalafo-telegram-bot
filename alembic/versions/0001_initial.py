"""Initial apartments and payment requests schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apartments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lalafo_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("rooms", sa.String(length=16), nullable=False),
        sa.Column("district", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("deposit", sa.Integer(), nullable=True),
        sa.Column("photo_urls", sa.JSON(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("publication_status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lalafo_id"),
    )
    op.create_index("ix_apartments_fingerprint", "apartments", ["fingerprint"])
    op.create_index("ix_apartments_lalafo_id", "apartments", ["lalafo_id"])
    op.create_table(
        "payment_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("apartment_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.BigInteger(), nullable=True),
        sa.Column("admin_message_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["apartment_id"], ["apartments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", "apartment_id", name="uq_payment_access"),
    )
    op.create_index("ix_payment_requests_apartment_id", "payment_requests", ["apartment_id"])
    op.create_index("ix_payment_requests_telegram_user_id", "payment_requests", ["telegram_user_id"])
    op.create_index("ix_payment_status_created", "payment_requests", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("payment_requests")
    op.drop_table("apartments")
