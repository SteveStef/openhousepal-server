"""add_host_request_counter_and_team

Revision ID: b2d5f8a1c4e7
Revises: a1c4e7f9b2d3
Create Date: 2026-07-28 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2d5f8a1c4e7'
down_revision: Union[str, None] = 'a1c4e7f9b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'open_house_team_broadcasts',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('requested_date', sa.Date(), nullable=False),
        sa.Column('requested_start_time', sa.Time(), nullable=False),
        sa.Column('requested_end_time', sa.Time(), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='open'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_open_house_team_broadcasts_listing_key'), 'open_house_team_broadcasts', ['listing_key'], unique=False)
    op.create_index(op.f('ix_open_house_team_broadcasts_owner_id'), 'open_house_team_broadcasts', ['owner_id'], unique=False)
    op.create_index(op.f('ix_open_house_team_broadcasts_status'), 'open_house_team_broadcasts', ['status'], unique=False)

    op.create_table(
        'open_house_team_members',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('member_agent_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['member_agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'member_agent_id', name='unique_team_owner_member'),
    )
    op.create_index(op.f('ix_open_house_team_members_owner_id'), 'open_house_team_members', ['owner_id'], unique=False)
    op.create_index(op.f('ix_open_house_team_members_member_agent_id'), 'open_house_team_members', ['member_agent_id'], unique=False)

    op.add_column('open_house_host_requests', sa.Column('counter_date', sa.Date(), nullable=True))
    op.add_column('open_house_host_requests', sa.Column('counter_start_time', sa.Time(), nullable=True))
    op.add_column('open_house_host_requests', sa.Column('counter_end_time', sa.Time(), nullable=True))
    op.add_column('open_house_host_requests', sa.Column('broadcast_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_open_house_host_requests_broadcast_id'), 'open_house_host_requests', ['broadcast_id'], unique=False)
    op.create_foreign_key(
        'fk_open_house_host_requests_broadcast_id',
        'open_house_host_requests', 'open_house_team_broadcasts',
        ['broadcast_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_open_house_host_requests_broadcast_id', 'open_house_host_requests', type_='foreignkey')
    op.drop_index(op.f('ix_open_house_host_requests_broadcast_id'), table_name='open_house_host_requests')
    op.drop_column('open_house_host_requests', 'broadcast_id')
    op.drop_column('open_house_host_requests', 'counter_end_time')
    op.drop_column('open_house_host_requests', 'counter_start_time')
    op.drop_column('open_house_host_requests', 'counter_date')

    op.drop_index(op.f('ix_open_house_team_members_member_agent_id'), table_name='open_house_team_members')
    op.drop_index(op.f('ix_open_house_team_members_owner_id'), table_name='open_house_team_members')
    op.drop_table('open_house_team_members')

    op.drop_index(op.f('ix_open_house_team_broadcasts_status'), table_name='open_house_team_broadcasts')
    op.drop_index(op.f('ix_open_house_team_broadcasts_owner_id'), table_name='open_house_team_broadcasts')
    op.drop_index(op.f('ix_open_house_team_broadcasts_listing_key'), table_name='open_house_team_broadcasts')
    op.drop_table('open_house_team_broadcasts')
