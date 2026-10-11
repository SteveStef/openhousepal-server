"""add open house cheat sheet snapshot

Revision ID: b3e8a7d2c4f9
Revises: a2d9f4c8b6e1
Create Date: 2026-08-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3e8a7d2c4f9"
down_revision: Union[str, None] = "a2d9f4c8b6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "open_house_events",
        sa.Column("cheat_sheet_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("open_house_events", "cheat_sheet_snapshot")
