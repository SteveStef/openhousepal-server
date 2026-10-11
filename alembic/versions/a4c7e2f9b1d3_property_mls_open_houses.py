"""properties.mls_open_houses - every upcoming Bright open house slot for a listing

Bright can have several open houses scheduled per listing (Sat + Sun, next weekend). The single
mls_open_house_date/_start_time/_end_time columns stay as "the soonest upcoming"; this list holds
all of them. Filled by the open house sync.

Revision ID: a4c7e2f9b1d3
Revises: f2b6d8a1c3e5
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c7e2f9b1d3"
down_revision: Union[str, None] = "f2b6d8a1c3e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("properties", sa.Column("mls_open_houses", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("properties", "mls_open_houses")
