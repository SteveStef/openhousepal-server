"""Add agent hub events

Revision ID: d7f3a5c9e1b2
Revises: c1a9e4b2f6d3
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f3a5c9e1b2'
down_revision: Union[str, None] = 'c1a9e4b2f6d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_hub_events',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_agent_hub_events_owner_id'), 'agent_hub_events', ['owner_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_hub_events_owner_id'), table_name='agent_hub_events')
    op.drop_table('agent_hub_events')
