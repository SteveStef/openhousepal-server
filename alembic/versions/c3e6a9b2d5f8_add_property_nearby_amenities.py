"""add_property_nearby_amenities

Revision ID: c3e6a9b2d5f8
Revises: b2d5f8a1c4e7
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3e6a9b2d5f8'
down_revision: Union[str, None] = 'b2d5f8a1c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('nearby_amenities', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('properties', sa.Column('nearby_amenities_cached_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('properties', 'nearby_amenities_cached_at')
    op.drop_column('properties', 'nearby_amenities')
