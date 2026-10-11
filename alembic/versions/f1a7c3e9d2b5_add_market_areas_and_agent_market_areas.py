"""add_market_areas_and_agent_market_areas

Revision ID: f1a7c3e9d2b5
Revises: d3f6a9c1b8e4
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f1a7c3e9d2b5'
down_revision: Union[str, None] = 'd3f6a9c1b8e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'market_areas',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('county', sa.String(), nullable=True),
        sa.Column('municipality_type', sa.String(), nullable=True),
        sa.Column('zip_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('centroid_lat', sa.Float(), nullable=True),
        sa.Column('centroid_lng', sa.Float(), nullable=True),
        sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('narrative', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('enrichment_status', sa.String(), nullable=False, server_default='PENDING'),
        sa.Column('last_enriched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
        sa.UniqueConstraint('name', 'state', name='uq_market_areas_name_state'),
    )
    op.create_index(op.f('ix_market_areas_slug'), 'market_areas', ['slug'])
    op.create_index(op.f('ix_market_areas_state'), 'market_areas', ['state'])

    op.create_table(
        'agent_market_areas',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('area_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['area_id'], ['market_areas.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'area_id', name='uq_agent_market_areas_owner_area'),
    )
    op.create_index(op.f('ix_agent_market_areas_owner_id'), 'agent_market_areas', ['owner_id'])
    op.create_index(op.f('ix_agent_market_areas_area_id'), 'agent_market_areas', ['area_id'])


def downgrade() -> None:
    op.drop_table('agent_market_areas')
    op.drop_table('market_areas')
