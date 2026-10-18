"""An agent's "I recommend this home for you" mark on a home in a client's Showcase - shown on the
card for both sides, sorted to the top, cleared when the client passes on it.

Revision ID: d5f7b9c1e3a4
Revises: c4e6a8b0d2f3
Create Date: 2026-10-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f7b9c1e3a4'
down_revision: Union[str, None] = 'c4e6a8b0d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('property_interactions', sa.Column('agent_recommended_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('property_interactions', 'agent_recommended_at')
