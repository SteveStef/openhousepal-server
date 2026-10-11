"""blogs.use_signature_card - close the article with the agent's Email Signature card (on by default)

Revision ID: d7f2b4c8e1a3
Revises: c6e1a9d3f5b7
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f2b4c8e1a3"
down_revision: Union[str, None] = "c6e1a9d3f5b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blogs", sa.Column("use_signature_card", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("blogs", "use_signature_card")
