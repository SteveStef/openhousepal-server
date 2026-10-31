"""Remember each home the agent sent a Showcase client by text / email

- collections.home_sends: newest-last list of {"at", "method" ('text' | 'email'), "ids"
  (property ids), "intro" (with the short message), "copied" (copied instead of opened)}.
  The agent's Showcase marks those homes "Sent" and the Send window shows how and when.

Nullable JSONB with no default, so adding it never rewrites the table.

Revision ID: c7e2a9d4f1b8
Revises: b3d8e1f4a6c2
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e2a9d4f1b8"
down_revision: Union[str, None] = "b3d8e1f4a6c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("home_sends", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "home_sends")
