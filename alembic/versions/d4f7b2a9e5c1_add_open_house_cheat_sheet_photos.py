"""add_open_house_cheat_sheet_photos

Revision ID: d4f7b2a9e5c1
Revises: c3e6a9b2d5f8
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4f7b2a9e5c1'
down_revision: Union[str, None] = 'c3e6a9b2d5f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_events', sa.Column('cheat_sheet_photos', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_events', 'cheat_sheet_photos')
