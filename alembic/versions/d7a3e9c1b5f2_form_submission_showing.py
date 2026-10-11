"""Schedule a Showing form: per-request showing state (home + agent-confirmed time)

Revision ID: d7a3e9c1b5f2
Revises: c1e5a7d9f2b4
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d7a3e9c1b5f2"
down_revision: Union[str, None] = "c1e5a7d9f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("form_submissions", sa.Column("showing", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("form_submissions", "showing")
