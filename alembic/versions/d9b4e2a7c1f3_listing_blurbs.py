"""listing_blurbs - the short AI "about this home" lines Studio designs reuse per agent per listing

Revision ID: d9b4e2a7c1f3
Revises: f5c2a9d4e7b1
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9b4e2a7c1f3'
down_revision = 'f5c2a9d4e7b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'listing_blurbs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source', sa.String(), nullable=False, server_default='ai'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('owner_id', 'key', name='uq_listing_blurbs_owner_key'),
    )
    op.create_index('ix_listing_blurbs_owner_id', 'listing_blurbs', ['owner_id'])


def downgrade():
    op.drop_index('ix_listing_blurbs_owner_id', table_name='listing_blurbs')
    op.drop_table('listing_blurbs')
