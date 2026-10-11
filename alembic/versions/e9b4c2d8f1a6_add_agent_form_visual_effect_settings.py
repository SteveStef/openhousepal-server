"""Add agent form visual effect settings

Revision ID: e9b4c2d8f1a6
Revises: d8f2c6a4b7e1
Create Date: 2026-08-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9b4c2d8f1a6'
down_revision: Union[str, None] = 'd8f2c6a4b7e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('background_effects', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('agent_forms', sa.Column('welcome_text_effects', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'welcome_text_effects')
    op.drop_column('agent_forms', 'background_effects')
