"""Add explore_nearby_cache

Revision ID: f2b7c93e4a1d
Revises: a1c4f9e27b3d
Create Date: 2026-08-16 04:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f2b7c93e4a1d'
down_revision: Union[str, None] = 'a1c4f9e27b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'explore_nearby_cache',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('cache_key', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cache_key'),
    )
    op.create_index('ix_explore_nearby_cache_cache_key', 'explore_nearby_cache', ['cache_key'])


def downgrade() -> None:
    op.drop_index('ix_explore_nearby_cache_cache_key', table_name='explore_nearby_cache')
    op.drop_table('explore_nearby_cache')
