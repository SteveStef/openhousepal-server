"""add_studio_blogs_and_design

Revision ID: a1b2c3d4e5f6
Revises: d4f7b2a9e5c1
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'd4f7b2a9e5c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'studio_brand_kits',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('primary_color', sa.String(), nullable=False, server_default='#111827'),
        sa.Column('secondary_color', sa.String(), nullable=False, server_default='#C9A24D'),
        sa.Column('accent_color', sa.String(), nullable=False, server_default='#816937'),
        sa.Column('heading_font', sa.String(), nullable=False, server_default='serif'),
        sa.Column('body_font', sa.String(), nullable=False, server_default='sans'),
        sa.Column('personal_logo_url', sa.String(), nullable=True),
        sa.Column('team_logo_url', sa.String(), nullable=True),
        sa.Column('brokerage_logo_url', sa.String(), nullable=True),
        sa.Column('disclaimer_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id'),
    )

    op.create_table(
        'design_projects',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('design_type', sa.String(), nullable=False),
        sa.Column('template_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='DRAFT'),
        sa.Column('linked_property', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('branding_mode', sa.String(), nullable=False, server_default='personal'),
        sa.Column('campaign_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_design_projects_owner_id'), 'design_projects', ['owner_id'])
    op.create_index(op.f('ix_design_projects_campaign_id'), 'design_projects', ['campaign_id'])

    op.create_table(
        'blogs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='DRAFT'),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('subtitle', sa.String(), nullable=True),
        sa.Column('cover_image_url', sa.String(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('linked_properties', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('seo_title', sa.String(), nullable=True),
        sa.Column('meta_description', sa.Text(), nullable=True),
        sa.Column('focus_keyword', sa.String(), nullable=True),
        sa.Column('social_title', sa.String(), nullable=True),
        sa.Column('social_description', sa.Text(), nullable=True),
        sa.Column('social_image_url', sa.String(), nullable=True),
        sa.Column('is_indexed', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('agent_hub_visible', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('agent_hub_featured', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'slug', name='uq_blogs_owner_slug'),
    )
    op.create_index(op.f('ix_blogs_owner_id'), 'blogs', ['owner_id'])
    op.create_index(op.f('ix_blogs_status'), 'blogs', ['status'])

    op.create_table(
        'blog_events',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('blog_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['blog_id'], ['blogs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_blog_events_blog_id'), 'blog_events', ['blog_id'])
    op.create_index(op.f('ix_blog_events_owner_id'), 'blog_events', ['owner_id'])


def downgrade() -> None:
    op.drop_table('blog_events')
    op.drop_table('blogs')
    op.drop_table('design_projects')
    op.drop_table('studio_brand_kits')
