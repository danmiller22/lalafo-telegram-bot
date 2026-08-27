"""Add isolated state for daily managed Lalafo publications."""

from alembic import op
import sqlalchemy as sa

revision = "0008_daily_featured"
down_revision = "0007_weekly_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_featured_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("source_apartment_id", sa.Integer(), nullable=False),
        sa.Column("source_lalafo_id", sa.BigInteger(), nullable=False),
        sa.Column("managed_lalafo_ad_id", sa.BigInteger()),
        sa.Column("managed_lalafo_ad_url", sa.Text()),
        sa.Column("campaign_id", sa.String(100)),
        sa.Column("campaign_status", sa.String(32), nullable=False, server_default="not_started"),
        sa.Column("campaign_daily_budget", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lalafo_publication_status", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("telegram_chat_id", sa.BigInteger()),
        sa.Column("last_telegram_repeat_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_apartment_id"], ["apartments.id"]),
        sa.UniqueConstraint("business_date", "slot", name="uq_featured_business_slot"),
        sa.UniqueConstraint("managed_lalafo_ad_id", name="uq_featured_managed_ad"),
    )
    op.create_index("ix_featured_business_date", "daily_featured_publications", ["business_date"])
    op.create_index("ix_featured_campaign_status", "daily_featured_publications", ["campaign_status"])
    op.create_index("ix_featured_source_apartment", "daily_featured_publications", ["source_apartment_id"])
    op.create_index("ix_featured_source_lalafo", "daily_featured_publications", ["source_lalafo_id"])
    op.create_table(
        "featured_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_apartment_id", sa.Integer(), nullable=False),
        sa.Column("source_lalafo_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="suggested"),
        sa.Column("selected_slot", sa.Integer()),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_apartment_id"], ["apartments.id"]),
        sa.UniqueConstraint("business_date", "source_lalafo_id", name="uq_featured_candidate_source"),
    )
    op.create_index("ix_featured_candidate_date_status", "featured_candidates", ["business_date", "status"])
    op.create_index("ix_featured_candidate_apartment", "featured_candidates", ["source_apartment_id"])


def downgrade() -> None:
    op.drop_table("featured_candidates")
    op.drop_table("daily_featured_publications")
