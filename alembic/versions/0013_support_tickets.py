"""Add persistent customer support tickets.

Revision ID: 0013_support_tickets
Revises: 0012_apartment_schedule
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_support_tickets"
down_revision = "0012_apartment_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("support_tickets"):
        return
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("first_name", sa.String(255)),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("admin_message_id", sa.BigInteger()),
        sa.Column("answer", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("answered_by", sa.BigInteger()),
    )
    op.create_index(
        "ix_support_tickets_telegram_user_id",
        "support_tickets",
        ["telegram_user_id"],
    )
    op.create_index(
        "ix_support_ticket_status_created",
        "support_tickets",
        ["status", "created_at"],
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("support_tickets"):
        op.drop_table("support_tickets")
