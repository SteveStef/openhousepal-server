"""add mls open house fields to properties

Revision ID: c1a7f9e4d2b3
Revises: 2a713142430e
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a7f9e4d2b3'
down_revision: Union[str, None] = '2a713142430e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('mls_open_house_date', sa.Date(), nullable=True))
    op.add_column('properties', sa.Column('mls_open_house_start_time', sa.Time(), nullable=True))
    op.add_column('properties', sa.Column('mls_open_house_end_time', sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column('properties', 'mls_open_house_end_time')
    op.drop_column('properties', 'mls_open_house_start_time')
    op.drop_column('properties', 'mls_open_house_date')
