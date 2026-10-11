"""Add second counter-time option to open_house_host_requests

Revision ID: c2e5f8a03b4d
Revises: b1d4e7f92a3c
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2e5f8a03b4d'
down_revision: Union[str, None] = 'b1d4e7f92a3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_host_requests', sa.Column('counter_date_2', sa.Date(), nullable=True))
    op.add_column('open_house_host_requests', sa.Column('counter_start_time_2', sa.Time(), nullable=True))
    op.add_column('open_house_host_requests', sa.Column('counter_end_time_2', sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_host_requests', 'counter_end_time_2')
    op.drop_column('open_house_host_requests', 'counter_start_time_2')
    op.drop_column('open_house_host_requests', 'counter_date_2')
