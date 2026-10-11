"""add_property_websites

Revision ID: 00108624cc77
Revises: b7d2f1a9c3e5
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '00108624cc77'
down_revision: Union[str, None] = 'b7d2f1a9c3e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'property_websites',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('listing_key', sa.String(), nullable=True),
        sa.Column('template_id', sa.String(), nullable=False, server_default='cinematic_luxury'),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='DRAFT'),
        sa.Column('property_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('draft_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('published_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index(op.f('ix_property_websites_owner_id'), 'property_websites', ['owner_id'])
    op.create_index(op.f('ix_property_websites_listing_key'), 'property_websites', ['listing_key'])
    op.create_index(op.f('ix_property_websites_slug'), 'property_websites', ['slug'])
    op.create_index('ix_property_websites_owner_id_status', 'property_websites', ['owner_id', 'status'])


def downgrade() -> None:
    op.drop_table('property_websites')
