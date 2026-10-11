"""Add support_tickets and support_ticket_messages

Revision ID: c1d4e8f2a6b9
Revises: b7c3e9f1a4d2
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d4e8f2a6b9'
down_revision: Union[str, None] = 'b7c3e9f1a4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='open'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('agent_last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('admin_last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_support_tickets_agent_id', 'support_tickets', ['agent_id'])
    op.create_index('ix_support_tickets_status', 'support_tickets', ['status'])

    op.create_table(
        'support_ticket_messages',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('ticket_id', sa.String(), nullable=False),
        sa.Column('sender_type', sa.String(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['support_tickets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_support_ticket_messages_ticket_id', 'support_ticket_messages', ['ticket_id'])


def downgrade() -> None:
    op.drop_index('ix_support_ticket_messages_ticket_id', table_name='support_ticket_messages')
    op.drop_table('support_ticket_messages')
    op.drop_index('ix_support_tickets_status', table_name='support_tickets')
    op.drop_index('ix_support_tickets_agent_id', table_name='support_tickets')
    op.drop_table('support_tickets')
