"""add storage_items (per-agent cross-app media storage ledger)

Revision ID: b7d2f1a9c3e5
Revises: f3a9b1c7d2e4
Create Date: 2026-09-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d2f1a9c3e5'
down_revision = 'f3a9b1c7d2e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'storage_items',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('s3_url', sa.String(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('s3_url', name='uq_storage_items_s3_url'),
    )
    op.create_index('ix_storage_items_owner_id', 'storage_items', ['owner_id'])
    op.create_index('ix_storage_items_s3_url', 'storage_items', ['s3_url'])


def downgrade() -> None:
    op.drop_index('ix_storage_items_s3_url', table_name='storage_items')
    op.drop_index('ix_storage_items_owner_id', table_name='storage_items')
    op.drop_table('storage_items')
