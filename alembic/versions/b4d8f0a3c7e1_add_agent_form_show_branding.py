"""Add agent form show_branding

Revision ID: b4d8f0a3c7e1
Revises: e5f7a9c2d4b6
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d8f0a3c7e1'
down_revision: Union[str, None] = 'e5f7a9c2d4b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('show_branding', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'show_branding')
