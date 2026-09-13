"""Add durable discovery inventory and publication queue.

Revision ID: 0015_apartment_inventory
Revises: 0014_lalafo_auto_reply
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_apartment_inventory"
down_revision = "0014_lalafo_auto_reply"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    apartment_columns = {item["name"] for item in inspector.get_columns("apartments")}
    if "owner_listing" not in apartment_columns:
        op.add_column("apartments", sa.Column("owner_listing", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "discovery_priority" not in apartment_columns:
        op.add_column("apartments", sa.Column("discovery_priority", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "last_seen_at" not in apartment_columns:
        op.add_column("apartments", sa.Column("last_seen_at", sa.DateTime(timezone=True)))

    inspector = sa.inspect(bind)
    if not inspector.has_table("apartment_inventory_queue"):
        op.create_table(
            "apartment_inventory_queue",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("apartment_id", sa.Integer(), sa.ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_key", sa.String(64), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("apartment_id", name="uq_inventory_apartment"),
        )
        op.create_index("ix_apartment_inventory_queue_apartment_id", "apartment_inventory_queue", ["apartment_id"])
        op.create_index("ix_inventory_due", "apartment_inventory_queue", ["status", "scheduled_at"])
        op.create_index("ix_inventory_window", "apartment_inventory_queue", ["window_key", "sequence"])

    if not inspector.has_table("apartment_discovery_runs"):
        op.create_table(
            "apartment_discovery_runs",
            sa.Column("period_key", sa.String(32), primary_key=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="running"),
            sa.Column("lease_until", sa.DateTime(timezone=True)),
            sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("queued_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("apartment_discovery_runs"):
        op.drop_table("apartment_discovery_runs")
    if inspector.has_table("apartment_inventory_queue"):
        op.drop_table("apartment_inventory_queue")
    apartment_columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("apartments")}
    for name in ("last_seen_at", "discovery_priority", "owner_listing"):
        if name in apartment_columns:
            op.drop_column("apartments", name)
