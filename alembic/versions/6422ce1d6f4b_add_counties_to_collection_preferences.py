"""add counties to collection preferences

Revision ID: 6422ce1d6f4b
Revises: 9e4a40cb1b7f
Create Date: 2026-08-16 21:42:03.907026

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6422ce1d6f4b'
down_revision: Union[str, None] = '9e4a40cb1b7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collection_preferences', sa.Column('counties', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('collection_preferences', 'counties')
