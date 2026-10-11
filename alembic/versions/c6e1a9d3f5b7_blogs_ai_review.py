"""blogs.ai_review - the AI pipeline's Verified report (claims checked, sources, fixes, scores)

Revision ID: c6e1a9d3f5b7
Revises: b5d8e2f4a6c1
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6e1a9d3f5b7"
down_revision: Union[str, None] = "b5d8e2f4a6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blogs", sa.Column("ai_review", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("blogs", "ai_review")
