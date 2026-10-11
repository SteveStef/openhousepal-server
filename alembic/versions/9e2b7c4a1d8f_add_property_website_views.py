"""add_property_website_views

Per-visit rows behind the Listing Spotlight visitor stats (unique visitors + total
visits, per site and overall). One row per visit, deduped ~30 min per (site, visitor)
in the service layer - see property_website_service.record_view.

Revision ID: 9e2b7c4a1d8f
Revises: 00108624cc77
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9e2b7c4a1d8f'
down_revision: Union[str, None] = '00108624cc77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'property_website_views',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('property_website_id', sa.String(), nullable=False),
        sa.Column('visitor_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['property_website_id'], ['property_websites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_property_website_views_property_website_id'), 'property_website_views', ['property_website_id'])
    op.create_index(op.f('ix_property_website_views_visitor_id'), 'property_website_views', ['visitor_id'])
    op.create_index(op.f('ix_property_website_views_created_at'), 'property_website_views', ['created_at'])
    op.create_index('ix_property_website_views_site_visitor', 'property_website_views', ['property_website_id', 'visitor_id'])


def downgrade() -> None:
    op.drop_table('property_website_views')
