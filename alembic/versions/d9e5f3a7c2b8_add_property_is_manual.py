"""Add property is_manual

Revision ID: d9e5f3a7c2b8
Revises: c4d8e2a6b1f3
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e5f3a7c2b8'
down_revision: Union[str, None] = 'c4d8e2a6b1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('is_manual', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('properties', 'is_manual')
