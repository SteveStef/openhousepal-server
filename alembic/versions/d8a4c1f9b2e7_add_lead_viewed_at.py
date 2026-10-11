"""add_lead_viewed_at

Revision ID: d8a4c1f9b2e7
Revises: c2e5f8a03b4d
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8a4c1f9b2e7'
down_revision: Union[str, None] = 'c2e5f8a03b4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('leads', 'viewed_at')
