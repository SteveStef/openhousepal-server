"""add property_interactions.agent_viewed_at

Revision ID: a7c3e9f1b4d6
Revises: f1a5c8e2b7d4
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a7c3e9f1b4d6"
down_revision: Union[str, None] = "f1a5c8e2b7d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("property_interactions", sa.Column("agent_viewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("property_interactions", "agent_viewed_at")
