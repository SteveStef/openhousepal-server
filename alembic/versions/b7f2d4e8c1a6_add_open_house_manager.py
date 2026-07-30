"""Add Open House Manager: listing MLS agent id, open house scheduling, host requests, listing notes

Revision ID: b7f2d4e8c1a6
Revises: a3c7e9f1b4d2
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f2d4e8c1a6'
down_revision: Union[str, None] = 'a3c7e9f1b4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('list_agent_mls_id', sa.String(), nullable=True))
    op.add_column('properties', sa.Column('co_list_agent_mls_id', sa.String(), nullable=True))
    op.create_index('ix_properties_list_agent_mls_id', 'properties', ['list_agent_mls_id'])
    op.create_index('ix_properties_co_list_agent_mls_id', 'properties', ['co_list_agent_mls_id'])

    op.add_column('open_house_events', sa.Column('scheduled_date', sa.Date(), nullable=True))
    op.add_column('open_house_events', sa.Column('scheduled_start_time', sa.Time(), nullable=True))
    op.add_column('open_house_events', sa.Column('scheduled_end_time', sa.Time(), nullable=True))
    op.add_column('open_house_events', sa.Column('listing_owner_agent_id', sa.String(), nullable=True))
    op.create_foreign_key('fk_open_house_events_listing_owner_agent_id', 'open_house_events', 'users', ['listing_owner_agent_id'], ['id'])
    op.create_index('ix_open_house_events_listing_key', 'open_house_events', ['listing_key'])

    op.create_table(
        'open_house_host_requests',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('listing_owner_agent_id', sa.String(), nullable=False),
        sa.Column('requesting_agent_id', sa.String(), nullable=False),
        sa.Column('requested_date', sa.Date(), nullable=False),
        sa.Column('requested_start_time', sa.Time(), nullable=False),
        sa.Column('requested_end_time', sa.Time(), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('open_house_event_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['listing_owner_agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requesting_agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['open_house_event_id'], ['open_house_events.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_open_house_host_requests_listing_key', 'open_house_host_requests', ['listing_key'])
    op.create_index('ix_open_house_host_requests_listing_owner_agent_id', 'open_house_host_requests', ['listing_owner_agent_id'])
    op.create_index('ix_open_house_host_requests_requesting_agent_id', 'open_house_host_requests', ['requesting_agent_id'])
    op.create_index('ix_open_house_host_requests_status', 'open_house_host_requests', ['status'])

    op.create_table(
        'open_house_listing_notes',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_priority_override', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('listing_key', name='uq_open_house_listing_notes_listing_key'),
    )
    op.create_index('ix_open_house_listing_notes_listing_key', 'open_house_listing_notes', ['listing_key'])
    op.create_index('ix_open_house_listing_notes_agent_id', 'open_house_listing_notes', ['agent_id'])


def downgrade() -> None:
    op.drop_index('ix_open_house_listing_notes_agent_id', table_name='open_house_listing_notes')
    op.drop_index('ix_open_house_listing_notes_listing_key', table_name='open_house_listing_notes')
    op.drop_table('open_house_listing_notes')

    op.drop_index('ix_open_house_host_requests_status', table_name='open_house_host_requests')
    op.drop_index('ix_open_house_host_requests_requesting_agent_id', table_name='open_house_host_requests')
    op.drop_index('ix_open_house_host_requests_listing_owner_agent_id', table_name='open_house_host_requests')
    op.drop_index('ix_open_house_host_requests_listing_key', table_name='open_house_host_requests')
    op.drop_table('open_house_host_requests')

    op.drop_index('ix_open_house_events_listing_key', table_name='open_house_events')
    op.drop_constraint('fk_open_house_events_listing_owner_agent_id', 'open_house_events', type_='foreignkey')
    op.drop_column('open_house_events', 'listing_owner_agent_id')
    op.drop_column('open_house_events', 'scheduled_end_time')
    op.drop_column('open_house_events', 'scheduled_start_time')
    op.drop_column('open_house_events', 'scheduled_date')

    op.drop_index('ix_properties_co_list_agent_mls_id', table_name='properties')
    op.drop_index('ix_properties_list_agent_mls_id', table_name='properties')
    op.drop_column('properties', 'co_list_agent_mls_id')
    op.drop_column('properties', 'list_agent_mls_id')
