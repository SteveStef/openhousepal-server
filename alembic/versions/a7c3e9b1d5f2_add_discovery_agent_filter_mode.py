"""add discovery agent filter mode

Revision ID: a7c3e9b1d5f2
Revises: f4b8d1e6c3a9
Create Date: 2026-08-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e9b1d5f2"
down_revision: Union[str, None] = "f4b8d1e6c3a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discovery_preferences",
        sa.Column("agent_filter_mode", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovery_preferences", "agent_filter_mode")
