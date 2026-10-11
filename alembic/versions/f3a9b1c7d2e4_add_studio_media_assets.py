"""add studio_media_assets (Studio AI Property Media tool)

Revision ID: f3a9b1c7d2e4
Revises: a1c4e8f2b6d9
Create Date: 2026-09-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'f3a9b1c7d2e4'
down_revision = 'a1c4e8f2b6d9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'studio_media_assets',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('group_id', sa.String(), nullable=False),
        sa.Column('parent_asset_id', sa.String(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='processing'),
        sa.Column('source_photo_url', sa.String(), nullable=False),
        sa.Column('source_kind', sa.String(), nullable=False),
        sa.Column('linked_property', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('provider', sa.String(), nullable=True),
        sa.Column('provider_model', sa.String(), nullable=True),
        sa.Column('provider_job_id', sa.String(), nullable=True),
        sa.Column('result_url', sa.String(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('prompt_version', sa.String(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_asset_id'], ['studio_media_assets.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_studio_media_assets_owner_id', 'studio_media_assets', ['owner_id'])
    op.create_index('ix_studio_media_assets_group_id', 'studio_media_assets', ['group_id'])


def downgrade() -> None:
    op.drop_index('ix_studio_media_assets_group_id', table_name='studio_media_assets')
    op.drop_index('ix_studio_media_assets_owner_id', table_name='studio_media_assets')
    op.drop_table('studio_media_assets')
