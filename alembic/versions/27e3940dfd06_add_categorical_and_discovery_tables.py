"""add_categorical_and_discovery_tables

Revision ID: 27e3940dfd06
Revises: 74476447af04
Create Date: 2026-05-13 15:30:05.551835

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '27e3940dfd06'
down_revision: Union[str, None] = '74476447af04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create brokerages table
    op.create_table('brokerages',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'state', name='unique_brokerage_name_state')
    )
    op.create_index(op.f('ix_brokerages_name'), 'brokerages', ['name'], unique=False)

    # 2. Create cities table
    op.create_table('cities',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'state', name='unique_city_name_state')
    )
    op.create_index(op.f('ix_cities_name'), 'cities', ['name'], unique=False)

    # 3. Create townships table
    op.create_table('townships',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'state', name='unique_township_name_state')
    )
    op.create_index(op.f('ix_townships_name'), 'townships', ['name'], unique=False)

    # 4. Create discovery_preferences table
    op.create_table('discovery_preferences',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('landmark_address', sa.String(), nullable=True),
        sa.Column('miles', sa.Float(), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('state', sa.String(), nullable=True),
        sa.Column('brokerages', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cities', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('townships', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('school_districts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id')
    )
    op.create_index(op.f('ix_discovery_preferences_latitude'), 'discovery_preferences', ['latitude'], unique=False)
    op.create_index(op.f('ix_discovery_preferences_longitude'), 'discovery_preferences', ['longitude'], unique=False)

    # Clean up old botched states (if any exist)
    op.execute("DROP INDEX IF EXISTS ix_discover_preferences_latitude")
    op.execute("DROP INDEX IF EXISTS ix_discover_preferences_longitude")
    op.execute("DROP TABLE IF EXISTS discover_preferences")
    
    # Ensure full_address index exists
    op.execute("DROP INDEX IF EXISTS idx_properties_full_address_trgm")
    # Using execute instead of op.create_index to avoid failure if already exists
    op.execute("CREATE INDEX IF NOT EXISTS ix_properties_full_address ON properties (full_address)")


def downgrade() -> None:
    op.drop_index(op.f('ix_discovery_preferences_longitude'), table_name='discovery_preferences')
    op.drop_index(op.f('ix_discovery_preferences_latitude'), table_name='discovery_preferences')
    op.drop_table('discovery_preferences')
    op.drop_index(op.f('ix_townships_name'), table_name='townships')
    op.drop_table('townships')
    op.drop_index(op.f('ix_cities_name'), table_name='cities')
    op.drop_table('cities')
    op.drop_index(op.f('ix_brokerages_name'), table_name='brokerages')
    op.drop_table('brokerages')
    op.drop_index(op.f('ix_properties_full_address'), table_name='properties')
