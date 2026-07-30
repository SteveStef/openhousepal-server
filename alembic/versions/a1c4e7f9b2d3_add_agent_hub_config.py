"""add_agent_hub_config

Revision ID: a1c4e7f9b2d3
Revises: d9e5f3a7c2b8
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f9b2d3'
down_revision: Union[str, None] = 'd9e5f3a7c2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'agent_hubs',
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('agent_hubs', 'config')
