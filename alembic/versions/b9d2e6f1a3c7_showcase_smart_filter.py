"""Showcase smart filter: the agent's AI must-haves on a Showcase search, and what the AI found per home

Revision ID: b9d2e6f1a3c7
Revises: f3c8a1d6b2e9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b9d2e6f1a3c7"
down_revision = "f3c8a1d6b2e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_preferences", sa.Column("ai_wants", postgresql.JSONB(), nullable=True))
    op.add_column("collection_properties", sa.Column("ai_match", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("collection_properties", "ai_match")
    op.drop_column("collection_preferences", "ai_wants")
