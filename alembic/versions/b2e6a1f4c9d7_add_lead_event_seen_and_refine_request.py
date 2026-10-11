"""add lead_events.agent_viewed_at and collections refine_request fields

Revision ID: b2e6a1f4c9d7
Revises: f3a1c9d5e7b2
Create Date: 2026-08-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2e6a1f4c9d7"
down_revision: Union[str, None] = "f3a1c9d5e7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lead_events", sa.Column("agent_viewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("collections", sa.Column("refine_request_message", sa.Text(), nullable=True))
    op.add_column("collections", sa.Column("refine_request_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "refine_request_at")
    op.drop_column("collections", "refine_request_message")
    op.drop_column("lead_events", "agent_viewed_at")
