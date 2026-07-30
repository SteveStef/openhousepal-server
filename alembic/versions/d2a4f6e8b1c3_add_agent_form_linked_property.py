"""Add linked_property JSONB snapshot column to agent_forms

Revision ID: d2a4f6e8b1c3
Revises: c7e2a91f4d3b
Create Date: 2026-07-27 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd2a4f6e8b1c3'
down_revision: Union[str, None] = 'c7e2a91f4d3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('linked_property', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_forms', 'linked_property')
