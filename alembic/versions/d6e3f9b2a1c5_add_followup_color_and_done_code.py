"""Follow-up calendar colour per agent + one-tap done code per task

Revision ID: d6e3f9b2a1c5
Revises: c5d2e8a1f7b4
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e3f9b2a1c5'
down_revision: Union[str, None] = 'c5d2e8a1f7b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('followup_color', sa.String(), nullable=True))
    op.add_column('agent_tasks', sa.Column('done_code', sa.String(), nullable=True))
    op.create_index('ix_agent_tasks_done_code', 'agent_tasks', ['done_code'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_agent_tasks_done_code', table_name='agent_tasks')
    op.drop_column('agent_tasks', 'done_code')
    op.drop_column('users', 'followup_color')
