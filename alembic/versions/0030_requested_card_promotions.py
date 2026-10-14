"""Durable requested-card promotion subscriptions and acknowledged receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0030_requested_card_promotions'
down_revision = '0029_manual_payment_review'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('requested_card_promotions',
        sa.Column('apartment_id', sa.Integer(), sa.ForeignKey('apartments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table('requested_card_publications',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('apartment_id', sa.Integer(), sa.ForeignKey('apartments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('telegram_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('seller_type', sa.String(16), nullable=False),
        sa.Column('district', sa.String(255)),
        sa.Column('price', sa.Integer(), nullable=False),
        sa.UniqueConstraint('telegram_chat_id','telegram_message_id',name='uq_requested_card_receipt'),
    )
    op.create_index('ix_requested_card_publications_apartment_id','requested_card_publications',['apartment_id'])
    op.create_index('ix_requested_card_publication_day','requested_card_publications',['published_at','apartment_id'])


def downgrade():
    op.drop_table('requested_card_publications')
    op.drop_table('requested_card_promotions')
