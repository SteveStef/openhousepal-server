"""Leads: the agent's star (favorite) on a lead

leads.is_favorite - starred from the Leads page (and the Visitors window), separate from starring
the lead's Showcase (collections.favorited_at). One new column with a constant default - nothing is
backfilled (Postgres adds it without rewriting the table).

Revision ID: e5a7c9b1d3f2
Revises: d4f6a8c0e2b1
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c9b1d3f2"
down_revision: Union[str, None] = "d4f6a8c0e2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("leads", "is_favorite")
