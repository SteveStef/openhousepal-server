"""site_redirects + sites.old_links_scan - old pages from the agent's previous website

When an agent connects their own domain, the site that used to live there still has pages in Google
(10/20: mainlinerealty.co/about and friends 404'd). old_links_service finds those addresses in the
Internet Archive and stores where each one goes on the new site.

New table + one nullable column without a default (catalog-only, instant) - no rewrite of any table.

Revision ID: b3e5a7c9d1f4
Revises: b8e2c4d6f1a3 (sites.google_search, another session's - same day)
Create Date: 2026-10-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b3e5a7c9d1f4'
down_revision: Union[str, None] = 'b8e2c4d6f1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sites', sa.Column('old_links_scan', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        'site_redirects',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('site_id', sa.String(), sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_path', sa.String(), nullable=False),
        sa.Column('target_path', sa.String(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('match_reason', sa.String(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('agent_approved', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'source_path', name='uq_site_redirects_site_source'),
    )
    op.create_index('ix_site_redirects_site_id', 'site_redirects', ['site_id'])


def downgrade() -> None:
    op.drop_index('ix_site_redirects_site_id', table_name='site_redirects')
    op.drop_table('site_redirects')
    op.drop_column('sites', 'old_links_scan')
