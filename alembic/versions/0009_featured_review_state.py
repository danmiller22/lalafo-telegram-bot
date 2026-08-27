"""Add isolated Telegram update cursor for the featured review bot."""

from alembic import op
import sqlalchemy as sa

revision = "0009_featured_review"
down_revision = "0008_daily_featured"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "featured_review_state",
        sa.Column("key", sa.String(50), primary_key=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "featured_candidates",
        sa.Column("source_payload", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("featured_candidates") as batch_op:
        batch_op.alter_column(
            "source_apartment_id", existing_type=sa.Integer(), nullable=True
        )
    op.execute("UPDATE featured_candidates SET source_payload = '{}' WHERE source_payload IS NULL")
    with op.batch_alter_table("featured_candidates") as batch_op:
        batch_op.alter_column(
            "source_payload", existing_type=sa.JSON(), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("featured_candidates") as batch_op:
        batch_op.alter_column(
            "source_apartment_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_column("source_payload")
    op.drop_table("featured_review_state")
