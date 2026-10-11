"""Admin activity tracker: when each agent was last in the app, and which days they used it

Revision ID: f9c3a7e1b5d2
Revises: e4b8c2f6a1d3
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f9c3a7e1b5d2"
down_revision: Union[str, None] = "e4b8c2f6a1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "agent_active_days",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("agent_active_days")
    op.drop_column("users", "last_seen_at")
