"""agent_market_areas.sort_order - the agent's own town order

The Areas list gets up/down arrows; the website's Neighborhoods page, home page towns and footer
follow that order. Nullable with no default, so adding it never rewrites the table.

Revision ID: d7e1b4a9c3f2
Revises: c4f8a2e6b9d1
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7e1b4a9c3f2"
down_revision: Union[str, None] = "c4f8a2e6b9d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_market_areas", sa.Column("sort_order", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_market_areas", "sort_order")
