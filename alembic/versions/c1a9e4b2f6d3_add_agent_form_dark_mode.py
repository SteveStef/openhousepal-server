"""Add agent form dark_mode

Revision ID: c1a9e4b2f6d3
Revises: b4d8f0a3c7e1
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a9e4b2f6d3'
down_revision: Union[str, None] = 'b4d8f0a3c7e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('dark_mode', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'dark_mode')
