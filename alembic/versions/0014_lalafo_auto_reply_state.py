"""Add durable Lalafo auto-reply jobs and cursors.

Revision ID: 0014_lalafo_auto_reply
Revises: 0013_support_tickets
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_lalafo_auto_reply"
down_revision = "0013_support_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("lalafo_auto_reply_jobs"):
        op.create_table(
            "lalafo_auto_reply_jobs",
            sa.Column("inbound_key", sa.String(600), primary_key=True),
            sa.Column("chat_key", sa.String(512), nullable=False),
            sa.Column("inbound_id", sa.String(255), nullable=False),
            sa.Column("inbound_time", sa.BigInteger(), nullable=False),
            sa.Column("ack", sa.String(36), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("needs_reconcile", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("first_attempt_at", sa.BigInteger()),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint("ack", name="uq_lalafo_auto_reply_job_ack"),
        )
        op.create_index(
            "ix_lalafo_auto_reply_jobs_ready",
            "lalafo_auto_reply_jobs",
            ["status", "next_attempt_at", "source", "inbound_time"],
        )
        op.create_index(
            "ix_lalafo_auto_reply_jobs_chat_order",
            "lalafo_auto_reply_jobs",
            ["chat_key", "inbound_time", "created_at", "inbound_key"],
        )
    if not inspector.has_table("lalafo_auto_reply_cursors"):
        op.create_table(
            "lalafo_auto_reply_cursors",
            sa.Column("chat_key", sa.String(512), primary_key=True),
            sa.Column("message_id", sa.String(255), nullable=False),
            sa.Column("message_time", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )
    if not inspector.has_table("lalafo_auto_reply_meta"):
        op.create_table(
            "lalafo_auto_reply_meta",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (
        "lalafo_auto_reply_meta",
        "lalafo_auto_reply_cursors",
        "lalafo_auto_reply_jobs",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
