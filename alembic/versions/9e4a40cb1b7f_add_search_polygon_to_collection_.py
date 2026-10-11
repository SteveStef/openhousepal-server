"""add_search_polygon_to_collection_preferences

Revision ID: 9e4a40cb1b7f
Revises: d1a4c7e0f3b8
Create Date: 2026-08-16 20:21:04.692208

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9e4a40cb1b7f'
down_revision: Union[str, None] = 'd1a4c7e0f3b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collection_preferences', sa.Column('search_polygon', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('collection_preferences', 'search_polygon')
