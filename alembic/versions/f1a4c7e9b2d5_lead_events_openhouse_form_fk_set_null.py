"""lead_events.open_house_event_id + form_id FKs: SET NULL on parent delete

Mirrors a1c4e8b2d9f3 (which did this for collection_id): deleting an open house
event or an agent form that a lead came through now nulls the pointer on
lead_events instead of raising a foreign-key violation and blocking the delete.

Revision ID: f1a4c7e9b2d5
Revises: 9e2b7c4a1d8f
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f1a4c7e9b2d5'
down_revision: Union[str, None] = '9e2b7c4a1d8f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('lead_events_open_house_event_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_open_house_event_id_fkey',
        'lead_events', 'open_house_events',
        ['open_house_event_id'], ['id'],
        ondelete='SET NULL',
    )

    op.drop_constraint('lead_events_form_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_form_id_fkey',
        'lead_events', 'agent_forms',
        ['form_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('lead_events_open_house_event_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_open_house_event_id_fkey',
        'lead_events', 'open_house_events',
        ['open_house_event_id'], ['id'],
    )

    op.drop_constraint('lead_events_form_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_form_id_fkey',
        'lead_events', 'agent_forms',
        ['form_id'], ['id'],
    )
