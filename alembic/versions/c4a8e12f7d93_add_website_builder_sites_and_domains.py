"""add_website_builder_sites_and_domains

Revision ID: c4a8e12f7d93
Revises: a1c4e8b2d9f3
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4a8e12f7d93'
down_revision: Union[str, None] = 'a1c4e8b2d9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sites',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('subdomain_slug', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='DRAFT'),
        sa.Column('draft_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('published_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id'),
        sa.UniqueConstraint('subdomain_slug'),
    )
    op.create_index(op.f('ix_sites_owner_id'), 'sites', ['owner_id'])
    op.create_index(op.f('ix_sites_subdomain_slug'), 'sites', ['subdomain_slug'])

    op.create_table(
        'site_domains',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('site_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('hostname', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='PENDING'),
        sa.Column('verification_token', sa.String(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('hostname'),
    )
    op.create_index(op.f('ix_site_domains_site_id'), 'site_domains', ['site_id'])
    op.create_index(op.f('ix_site_domains_owner_id'), 'site_domains', ['owner_id'])
    op.create_index(op.f('ix_site_domains_hostname'), 'site_domains', ['hostname'])


def downgrade() -> None:
    op.drop_table('site_domains')
    op.drop_table('sites')
