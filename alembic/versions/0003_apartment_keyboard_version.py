"""Track the rendered apartment keyboard version."""

from alembic import op
import sqlalchemy as sa

revision = "0003_apartment_keyboard_version"
down_revision = "0002_wanted_ads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("apartments")}
    if "keyboard_version" in columns:
        return
    op.add_column(
        "apartments",
        sa.Column(
            "keyboard_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("apartments")}
    if "keyboard_version" not in columns:
        return
    op.drop_column("apartments", "keyboard_version")
