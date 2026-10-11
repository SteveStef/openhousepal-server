"""add match score compare property to collection preferences

Revision ID: b15a8a1100e5
Revises: 6422ce1d6f4b
Create Date: 2026-08-16 23:57:02.511237

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b15a8a1100e5'
down_revision: Union[str, None] = '6422ce1d6f4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collection_preferences', sa.Column('compare_property_listing_key', sa.String(), nullable=True))
    op.add_column('collection_preferences', sa.Column('show_match_score', sa.Boolean(), server_default='true', nullable=False))


def downgrade() -> None:
    op.drop_column('collection_preferences', 'show_match_score')
    op.drop_column('collection_preferences', 'compare_property_listing_key')
