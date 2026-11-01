"""Discovery search: a max price next to the min price (owner, 11/01)

- discovery_preferences.max_price: homes priced above it are left out of Discovery (and the AI
  helper's "what can I host" list, which reads the same search).

Nullable with no default, so adding it never rewrites the table.

Revision ID: d4a8c2e6f9b1
Revises: c7e2a9d4f1b8
Create Date: 2026-11-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8c2e6f9b1"
down_revision: Union[str, None] = "c7e2a9d4f1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("discovery_preferences", sa.Column("max_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("discovery_preferences", "max_price")
