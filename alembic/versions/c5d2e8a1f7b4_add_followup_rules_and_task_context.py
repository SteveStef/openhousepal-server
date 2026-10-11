"""Add per-agent automatic follow-up rules + task context line

Revision ID: c5d2e8a1f7b4
Revises: f7a2c9e4b8d6
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5d2e8a1f7b4'
down_revision: Union[str, None] = 'f7a2c9e4b8d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('followup_rules', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('agent_tasks', sa.Column('context', sa.String(), nullable=True))
    # Auto follow-ups are now whole-day reminders (no 9am slot on the agent's calendar).
    op.execute("UPDATE agent_tasks SET all_day = true WHERE auto_source IS NOT NULL AND completed_at IS NULL")


def downgrade() -> None:
    op.drop_column('agent_tasks', 'context')
    op.drop_column('users', 'followup_rules')
