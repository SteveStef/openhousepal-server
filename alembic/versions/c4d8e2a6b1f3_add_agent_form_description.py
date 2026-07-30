"""Add agent form description

Revision ID: c4d8e2a6b1f3
Revises: b7f2d4e8c1a6
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2a6b1f3'
down_revision: Union[str, None] = 'b7f2d4e8c1a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_forms', 'description')
