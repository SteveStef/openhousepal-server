"""add cma_reports.ai_analysis

Revision ID: 0793fc4c2145
Revises: b2e6a1f4c9d7
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0793fc4c2145"
down_revision: Union[str, None] = "b2e6a1f4c9d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cma_reports", sa.Column("ai_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("cma_reports", "ai_analysis")
