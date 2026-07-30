"""Drop agent_forms owner_id+form_type unique constraint (agents can now have multiple custom forms)

Revision ID: c7e2a91f4d3b
Revises: f4b8e21c9a06
Create Date: 2026-07-27 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2a91f4d3b'
down_revision: Union[str, None] = 'f4b8e21c9a06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('uq_agent_forms_owner_type', 'agent_forms', type_='unique')


def downgrade() -> None:
    op.create_unique_constraint('uq_agent_forms_owner_type', 'agent_forms', ['owner_id', 'form_type'])
