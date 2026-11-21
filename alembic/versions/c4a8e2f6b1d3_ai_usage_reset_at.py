"""users.ai_usage_reset_at - admin "Reset AI usage" (11/20)

Revision ID: c4a8e2f6b1d3
Revises: b8e4d2f6a1c9
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa


revision = "c4a8e2f6b1d3"
down_revision = "b8e4d2f6a1c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("ai_usage_reset_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "ai_usage_reset_at")
