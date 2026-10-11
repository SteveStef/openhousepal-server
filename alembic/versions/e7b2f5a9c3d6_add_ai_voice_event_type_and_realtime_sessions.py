"""add event_type to ai_usage_events and ai_realtime_sessions table

Revision ID: e7b2f5a9c3d6
Revises: d5e9c3a7f1b8
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2f5a9c3d6'
down_revision: Union[str, None] = 'd5e9c3a7f1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ai_usage_events',
        sa.Column('event_type', sa.String(), server_default='chat', nullable=False),
    )

    op.create_table(
        'ai_realtime_sessions',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_reason', sa.String(), nullable=True),
        sa.Column('tool_call_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('estimated_cost_usd', sa.Float(), server_default=sa.text('0'), nullable=False),
    )
    op.create_index('ix_ai_realtime_sessions_owner_id', 'ai_realtime_sessions', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_ai_realtime_sessions_owner_id', table_name='ai_realtime_sessions')
    op.drop_table('ai_realtime_sessions')
    op.drop_column('ai_usage_events', 'event_type')
