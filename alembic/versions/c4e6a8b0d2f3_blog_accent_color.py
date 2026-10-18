"""An article's accent color for its Classic look - the same choices as the Agent Hub's Classic
(Original gold + the Classic accents), so Classic means the same thing everywhere.

Revision ID: c4e6a8b0d2f3
Revises: b7d2e4f6a8c1
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e6a8b0d2f3'
down_revision: Union[str, None] = 'b7d2e4f6a8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('blogs', sa.Column('accent_color', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('blogs', 'accent_color')
