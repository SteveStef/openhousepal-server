"""add_site_domain_redirect_target

Revision ID: d3f6a9c1b8e4
Revises: c4a8e12f7d93
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3f6a9c1b8e4'
down_revision: Union[str, None] = 'c4a8e12f7d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('site_domains', sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('site_domains', sa.Column('last_error', sa.String(), nullable=True))
    op.add_column('site_domains', sa.Column('redirect_target_domain_id', sa.String(), nullable=True))
    op.create_foreign_key(
        'fk_site_domains_redirect_target', 'site_domains', 'site_domains',
        ['redirect_target_domain_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index(op.f('ix_site_domains_redirect_target_domain_id'), 'site_domains', ['redirect_target_domain_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_site_domains_redirect_target_domain_id'), table_name='site_domains')
    op.drop_constraint('fk_site_domains_redirect_target', 'site_domains', type_='foreignkey')
    op.drop_column('site_domains', 'redirect_target_domain_id')
    op.drop_column('site_domains', 'last_error')
    op.drop_column('site_domains', 'last_checked_at')
