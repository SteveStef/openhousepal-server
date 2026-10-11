"""add agent_reviews

Revision ID: d1f6a2c8e470
Revises: c7e4a1f9b820
Create Date: 2026-09-08 00:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1f6a2c8e470"
down_revision: Union[str, None] = "c7e4a1f9b820"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_reviews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("reviewer_name", sa.String(), nullable=False),
        sa.Column("reviewer_email", sa.String(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="direct"),
        sa.Column("status", sa.String(), nullable=False, server_default="published"),
        sa.Column("posted_google_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_zillow_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_reviews_owner_id", "agent_reviews", ["owner_id"], unique=False)
    op.create_index("ix_agent_reviews_created_at", "agent_reviews", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_table("agent_reviews")
