"""add comment read tracking (is_agent, agent/visitor_comments_read_at)

Revision ID: b2e7f4a9c1d3
Revises: a7c3e9f1b4d6
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2e7f4a9c1d3"
down_revision: Union[str, None] = "a7c3e9f1b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("property_interactions", sa.Column("agent_comments_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("property_interactions", sa.Column("visitor_comments_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("property_comments", sa.Column("is_agent", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("property_comments", "is_agent", server_default=None)


def downgrade() -> None:
    op.drop_column("property_comments", "is_agent")
    op.drop_column("property_interactions", "visitor_comments_read_at")
    op.drop_column("property_interactions", "agent_comments_read_at")
