"""Add agent form redirect_to_agent_hub

Revision ID: e2b6c4a8f0d5
Revises: d7f3a5c9e1b2
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b6c4a8f0d5'
down_revision: Union[str, None] = 'd7f3a5c9e1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('redirect_to_agent_hub', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'redirect_to_agent_hub')
