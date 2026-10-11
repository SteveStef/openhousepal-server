"""blogs.idx_visible - per-article switch for showing it on the agent's IDX website

Revision ID: b5d8e2f4a6c1
Revises: a3c7e9f1b2d4
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d8e2f4a6c1"
down_revision: Union[str, None] = "a3c7e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blogs", sa.Column("idx_visible", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("blogs", "idx_visible")
