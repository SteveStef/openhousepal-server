"""add_agent_hub_event_visitor_id

Revision ID: b3d8f2a5c1e7
Revises: a2c7f9e1b4d8
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3d8f2a5c1e7'
down_revision: Union[str, None] = 'a2c7f9e1b4d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_hub_events', sa.Column('visitor_id', sa.String(), nullable=True))
    op.create_index('ix_agent_hub_events_visitor_id', 'agent_hub_events', ['visitor_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_hub_events_visitor_id', table_name='agent_hub_events')
    op.drop_column('agent_hub_events', 'visitor_id')
