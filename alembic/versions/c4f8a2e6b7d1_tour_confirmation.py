"""add property_tours confirmed_date/confirmed_time

Revision ID: c4f8a2e6b7d1
Revises: b2e7f4a9c1d3
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4f8a2e6b7d1"
down_revision: Union[str, None] = "b2e7f4a9c1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("property_tours", sa.Column("confirmed_date", sa.String(), nullable=True))
    op.add_column("property_tours", sa.Column("confirmed_time", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("property_tours", "confirmed_time")
    op.drop_column("property_tours", "confirmed_date")
