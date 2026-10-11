"""add_lead_showcase_deleted_at

Revision ID: d8f2c6a4b7e1
Revises: c4e7a1b9d3f2
Create Date: 2026-07-31 00:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8f2c6a4b7e1'
down_revision: Union[str, None] = 'c4e7a1b9d3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('showcase_deleted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('leads', 'showcase_deleted_at')
