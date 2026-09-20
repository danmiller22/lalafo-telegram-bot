"""Track owner, realtor, and unknown sellers separately.

Revision ID: 0016_apartment_seller_type
Revises: 0015_apartment_inventory
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_apartment_seller_type"
down_revision = "0015_apartment_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("apartments")}
    if "seller_type" not in columns:
        op.add_column(
            "apartments",
            sa.Column(
                "seller_type",
                sa.String(16),
                nullable=False,
                server_default="unknown",
            ),
        )
        op.execute(
            "UPDATE apartments SET seller_type = 'owner' "
            "WHERE owner_listing = TRUE"
        )
        # Older rows only had a boolean owner flag. Conservatively classify
        # the remaining legacy stock as realtor until the next detail refresh
        # can distinguish a real agent from an unspecified seller.
        op.execute(
            "UPDATE apartments SET seller_type = 'realtor' "
            "WHERE owner_listing = FALSE"
        )


def downgrade() -> None:
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("apartments")
    }
    if "seller_type" in columns:
        op.drop_column("apartments", "seller_type")
