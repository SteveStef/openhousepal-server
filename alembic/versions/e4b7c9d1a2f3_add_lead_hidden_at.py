"""add lead hidden_at

Revision ID: e4b7c9d1a2f3
Revises: c8a1f0b2d9e4
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e4b7c9d1a2f3"
down_revision: Union[str, None] = "c8a1f0b2d9e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_leads_hidden_at", "leads", ["hidden_at"])


def downgrade() -> None:
    op.drop_index("ix_leads_hidden_at", table_name="leads")
    op.drop_column("leads", "hidden_at")
