"""Store whether an apartment is rented without subletting."""

from alembic import op
import sqlalchemy as sa


revision = "0005_subletting_status"
down_revision = "0004_phone_source_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("apartments")}
    if "no_subletting" in columns:
        return
    op.add_column(
        "apartments",
        sa.Column(
            "no_subletting",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("apartments")}
    if "no_subletting" not in columns:
        return
    op.drop_column("apartments", "no_subletting")
