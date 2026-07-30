"""Add agent form branding_layout

Revision ID: a3c7e9f1b4d2
Revises: f1a4b8c2d6e9
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c7e9f1b4d2'
down_revision: Union[str, None] = 'f1a4b8c2d6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('branding_layout', sa.String(), server_default='split', nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'branding_layout')
