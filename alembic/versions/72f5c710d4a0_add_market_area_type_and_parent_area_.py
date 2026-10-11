"""add market area type and parent area name

Revision ID: 72f5c710d4a0
Revises: 22728c527592
Create Date: 2026-08-14 09:45:33.283932

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '72f5c710d4a0'
down_revision: Union[str, None] = '22728c527592'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('market_areas', sa.Column('area_type', sa.String(), server_default='city', nullable=False))
    op.add_column('market_areas', sa.Column('parent_area_name', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('market_areas', 'parent_area_name')
    op.drop_column('market_areas', 'area_type')
