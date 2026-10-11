"""add discovery excluded agents

Revision ID: f4b8d1e6c3a9
Revises: e6c2a9f4b1d7
Create Date: 2026-08-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4b8d1e6c3a9"
down_revision: Union[str, None] = "e6c2a9f4b1d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discovery_preferences",
        sa.Column("excluded_agents", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovery_preferences", "excluded_agents")
