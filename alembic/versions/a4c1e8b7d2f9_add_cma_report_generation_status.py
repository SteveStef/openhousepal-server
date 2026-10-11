"""add cma_reports.generation_status / generation_started_at

Revision ID: a4c1e8b7d2f9
Revises: b8c3d5e7f1a2
Create Date: 2026-09-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4c1e8b7d2f9"
down_revision: Union[str, None] = "b8c3d5e7f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cma_reports", sa.Column("generation_status", sa.String(), nullable=True))
    op.add_column("cma_reports", sa.Column("generation_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_cma_reports_generation_status", "cma_reports", ["generation_status"])


def downgrade() -> None:
    op.drop_index("ix_cma_reports_generation_status", table_name="cma_reports")
    op.drop_column("cma_reports", "generation_started_at")
    op.drop_column("cma_reports", "generation_status")
