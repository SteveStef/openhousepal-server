"""add open house cover image position

Revision ID: e6c2a9f4b1d7
Revises: d5a1c8f3e9b2
Create Date: 2026-08-04 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e6c2a9f4b1d7"
down_revision: Union[str, None] = "d5a1c8f3e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "open_house_events",
        sa.Column("cover_image_position", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("open_house_events", "cover_image_position")
