"""Add link column to notifications

Revision ID: 8d3ce7050151
Revises: 1a8a112b8e54
Create Date: 2025-11-23 09:19:59.957268

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d3ce7050151'
down_revision: Union[str, None] = '1a8a112b8e54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('link', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('notifications', 'link')
