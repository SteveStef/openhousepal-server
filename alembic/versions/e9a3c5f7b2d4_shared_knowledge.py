"""shared_knowledge - researched facts above town level (e.g. state:PA), reused across articles

Revision ID: e9a3c5f7b2d4
Revises: d7f2b4c8e1a3
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9a3c5f7b2d4"
down_revision: Union[str, None] = "d7f2b4c8e1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shared_knowledge",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shared_knowledge_scope", "shared_knowledge", ["scope"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_shared_knowledge_scope", table_name="shared_knowledge")
    op.drop_table("shared_knowledge")
