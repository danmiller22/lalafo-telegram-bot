"""Durable quota ledger for personal apartment selection."""
from alembic import op
import sqlalchemy as sa

revision = "0037_personal_match_reservations"
down_revision = "0036_search_profile_seller_types"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("personal_match_reservations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_history_id", sa.Integer(), sa.ForeignKey("payment_history.id"), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("delivery_id", name="uq_personal_delivery"))
    op.create_index("ix_personal_match_reservations_payment_history_id", "personal_match_reservations", ["payment_history_id"])


def downgrade():
    op.drop_table("personal_match_reservations")
