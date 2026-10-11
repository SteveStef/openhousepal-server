"""Add counter_message to open_house_host_requests

Revision ID: b1d4e7f92a3c
Revises: a7c3e9b1d5f2
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1d4e7f92a3c'
down_revision: Union[str, None] = 'a7c3e9b1d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_host_requests', sa.Column('counter_message', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_host_requests', 'counter_message')
