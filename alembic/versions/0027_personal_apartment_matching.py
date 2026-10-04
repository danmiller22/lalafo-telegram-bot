"""Add personal apartment filters, delivery outbox and funnel events."""

from alembic import op
import sqlalchemy as sa


revision = "0027_personal_matching"
down_revision = "0026_lifetime_payment_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apartment_search_profiles",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("rooms", sa.JSON(), nullable=False),
        sa.Column("districts", sa.JSON(), nullable=False),
        sa.Column("all_districts", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_budget", sa.Integer(), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(32), nullable=False, server_default="telegram"),
        sa.Column("notifications_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "apartment_match_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "apartment_id",
            sa.Integer(),
            sa.ForeignKey("apartments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default="notification"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_user_id", "apartment_id", name="uq_match_user_apartment"),
    )
    op.create_index(
        "ix_match_delivery_due",
        "apartment_match_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_apartment_match_deliveries_telegram_user_id",
        "apartment_match_deliveries",
        ["telegram_user_id"],
    )
    op.create_index(
        "ix_apartment_match_deliveries_apartment_id",
        "apartment_match_deliveries",
        ["apartment_id"],
    )
    op.create_table(
        "customer_funnel_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_name", sa.String(40), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="telegram"),
        sa.Column("apartment_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_funnel_event_name_created",
        "customer_funnel_events",
        ["event_name", "created_at"],
    )
    op.create_index(
        "ix_funnel_user_created",
        "customer_funnel_events",
        ["telegram_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("customer_funnel_events")
    op.drop_table("apartment_match_deliveries")
    op.drop_table("apartment_search_profiles")
