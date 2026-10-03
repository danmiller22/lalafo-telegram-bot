"""Create the payment ledger when missing and allow access without an expiry."""

from alembic import op
import sqlalchemy as sa

revision = "0026_lifetime_payment_access"
down_revision = "0025_availability_and_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older deployments created this ledger through init_db, while a fresh
    # Alembic database has no ledger yet.
    if not sa.inspect(op.get_bind()).has_table("payment_history"):
        op.create_table(
            "payment_history",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("payment_request_id", sa.Integer(), nullable=False),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(64)),
            sa.Column("first_name", sa.String(255)),
            sa.Column("apartment_id", sa.Integer(), nullable=False),
            sa.Column("plan", sa.String(16), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("provider_payment_id", sa.String(64), nullable=False),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_payment_history_user_paid", "payment_history", ["telegram_user_id", "paid_at"])
        op.create_index("ix_payment_history_provider_id", "payment_history", ["provider_payment_id"], unique=True)
    else:
        with op.batch_alter_table("payment_history") as batch:
            batch.alter_column("access_expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade() -> None:
    # Preserve lifetime records when restoring the old non-null schema.
    op.execute("UPDATE payment_history SET access_expires_at = '9999-12-31 00:00:00+00' WHERE access_expires_at IS NULL")
    with op.batch_alter_table("payment_history") as batch:
        batch.alter_column("access_expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
