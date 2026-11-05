"""Forms: where people go after the thank-you screen (owner, 11/04)

- agent_forms.redirect_mode: NULL = the old Agent Hub switch decides; agent_hub | sales | website |
  property | none | custom.
- agent_forms.redirect_url: the custom link.

Nullable with no default, so adding them never rewrites the table.

Revision ID: c1f4a9d2e7b6
Revises: b9c4e2a7d1f3
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1f4a9d2e7b6"
down_revision: Union[str, None] = "b9c4e2a7d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_forms", sa.Column("redirect_mode", sa.String(length=20), nullable=True))
    op.add_column("agent_forms", sa.Column("redirect_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_forms", "redirect_url")
    op.drop_column("agent_forms", "redirect_mode")
