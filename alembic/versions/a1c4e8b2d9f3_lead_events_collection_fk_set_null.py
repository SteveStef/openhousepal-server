"""lead_events.collection_id FK: SET NULL on collection delete

Revision ID: a1c4e8b2d9f3
Revises: e7b2f5a9c3d6
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1c4e8b2d9f3'
down_revision: Union[str, None] = 'e7b2f5a9c3d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('lead_events_collection_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_collection_id_fkey',
        'lead_events', 'collections',
        ['collection_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('lead_events_collection_id_fkey', 'lead_events', type_='foreignkey')
    op.create_foreign_key(
        'lead_events_collection_id_fkey',
        'lead_events', 'collections',
        ['collection_id'], ['id'],
    )
